from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from html import escape
from math import ceil

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import BufferedInputFile, CallbackQuery
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import callbacks as cb, keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.admin import router
from app.handlers.common import require_admin, require_writer
from app.models import ImportBatch, ImportRow, Pharmacy, Shift
from app.services.shift_schedule_tools import get_shift_times
from app.services.smart_schedule import (
    DAY,
    EVENING,
    SMART_SOURCE_TYPE,
    analyze_batch,
    default_period,
    draft_shift_views,
    fixed_from_batch,
    generate_import_rows,
    pharmacy_year_statistics,
)
from app.services.word_export import WordExportError, build_official_word_schedule
from app.telegram_utils import answer_callback, safe_edit
from app.utils import as_local, format_date_ar, utcnow


SMART_HOME = "a:smart"
PAGE_SIZE = 7
STATS_PAGE_SIZE = 6


def _install_admin_entry_button() -> None:
    current = keyboards.admin_home
    if getattr(current, "_smart_schedule_wrapped", False):
        return

    def wrapped_admin_home():
        markup = current()
        rows = [list(row) for row in markup.inline_keyboard]
        new_button = keyboards.button(
            "🧠 مولّد الجداول الذكي",
            SMART_HOME,
            ButtonStyle.SUCCESS,
        )
        insert_at = 4 if len(rows) >= 4 else len(rows)
        rows.insert(insert_at, [new_button])
        return keyboards.keyboard(rows)

    setattr(wrapped_admin_home, "_smart_schedule_wrapped", True)
    keyboards.admin_home = wrapped_admin_home


_install_admin_entry_button()


def _explain(*lines: str) -> list[str]:
    return [f"🔹 {line}" for line in lines]


def _date_text(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _row_period(row: ImportRow, timezone) -> str:
    period = str((row.raw_data or {}).get("period") or "")
    if period in {DAY, EVENING}:
        return period
    if row.start_at is None:
        return DAY
    return DAY if as_local(row.start_at, timezone).hour < 18 else EVENING


def _row_name(row: ImportRow) -> str:
    if row.matched_pharmacy:
        return row.matched_pharmacy.name
    return row.raw_pharmacy_name


def _batch_days(batch: ImportBatch, timezone) -> list[tuple[date, ImportRow | None, ImportRow | None]]:
    grouped: dict[date, dict[str, ImportRow]] = defaultdict(dict)
    for row in batch.rows:
        if row.start_at is None:
            continue
        duty_date = as_local(row.start_at, timezone).date()
        grouped[duty_date][_row_period(row, timezone)] = row
    return [
        (duty_date, values.get(DAY), values.get(EVENING))
        for duty_date, values in sorted(grouped.items())
    ]


async def _latest_smart_draft(session) -> ImportBatch | None:
    return await session.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows).selectinload(ImportRow.matched_pharmacy))
        .where(
            ImportBatch.source_type == SMART_SOURCE_TYPE,
            ImportBatch.status == "draft",
        )
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(1)
    )


async def _smart_published_batches(session, limit: int = 50) -> list[ImportBatch]:
    result = await session.scalars(
        select(ImportBatch)
        .where(
            ImportBatch.source_type == SMART_SOURCE_TYPE,
            ImportBatch.status == "published",
        )
        .order_by(ImportBatch.published_at.desc(), ImportBatch.id.desc())
        .limit(limit)
    )
    return list(result)


async def _render_smart_home(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        latest_end = await repositories.latest_shift_end(session)
        draft = await _latest_smart_draft(session)
        published = await _smart_published_batches(session)
    if latest_end:
        latest_local = as_local(latest_end, settings.timezone).date()
        latest_line = f"📋 آخر يوم منشور: {_date_text(latest_local)}"
        next_line = f"⏭️ بداية الجدول المقترحة: {_date_text(latest_local + timedelta(days=1))}"
    else:
        latest_line = "📋 لا يوجد جدول منشور حالياً."
        next_line = "⏭️ سيبدأ الجدول المقترح من تاريخ اليوم."

    button_help = [
        "✨ إنشاء جدول جديد — ينشئ مسودة ذكية فقط، ولا ينشر شيئاً تلقائياً.",
        "📊 إحصائيات الصيدليات — يعرض كل مناوبات الصيدليات والنهاري والليلي والجمعات والعدالة.",
        "📚 الجداول الذكية السابقة — يفتح الجداول التي تم اعتمادها من هذا القسم.",
        "⚙️ قواعد التوزيع — يوضح القواعد التي يحلل بها البوت كل اختيار.",
        "🔙 رجوع — يرجع إلى لوحة الإدارة.",
    ]
    rows = [[keyboards.button("✨ إنشاء جدول جديد", "a:smart:new", ButtonStyle.SUCCESS)]]
    if draft:
        button_help.insert(1, "📝 فتح المسودة الحالية — يكمل المراجعة والتعديل قبل النشر.")
        rows.append([keyboards.button("📝 فتح المسودة الحالية", f"a:smart:draft:{draft.id}", ButtonStyle.PRIMARY)])
    rows.extend(
        [
            [keyboards.button("📊 إحصائيات الصيدليات", "a:smart:stats", ButtonStyle.PRIMARY)],
            [keyboards.button("📚 الجداول الذكية السابقة", "a:smart:history:0", ButtonStyle.PRIMARY)],
            [keyboards.button("⚙️ قواعد التوزيع", "a:smart:rules", ButtonStyle.PRIMARY)],
            [keyboards.button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "مولّد الجداول الذكي",
            "هذا القسم ينشئ الجدول كمسودة، يحلله، يسمح لك بمراجعته وتعديله، ثم لا ينشره إلا بعد تأكيدك النهائي.",
            stats=[latest_line, next_line, f"📚 جداول ذكية منشورة: {len(published)}", *_explain(*button_help)],
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart")
async def smart_home(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    await _render_smart_home(callback, db, settings)


@router.callback_query(F.data == "a:smart:new")
async def smart_new(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    async with db.session_factory() as session:
        draft = await _latest_smart_draft(session)
        latest_end = await repositories.latest_shift_end(session)
    if draft:
        await answer_callback(callback, "يوجد مسودة ذكية حالياً. افتحها أو احذفها قبل إنشاء مسودة جديدة.", alert=True)
        await _render_draft(callback, db, settings, draft.id)
        return
    start_date, end_date = default_period(latest_end, settings.timezone)
    await _render_range(callback, db, start_date, end_date)


async def _render_range(callback: CallbackQuery, db: Database, start_date: date, end_date: date) -> None:
    if await require_writer(callback, db) is None:
        return
    days = (end_date - start_date).days + 1
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إنشاء جدول ذكي جديد",
            "راجع تاريخ البداية والنهاية أولاً. عند الضغط على التوليد سيُنشأ جدول كامل كمسودة فقط.",
            stats=[
                f"📅 البداية: {_date_text(start_date)}",
                f"📅 النهاية: {_date_text(end_date)}",
                f"🗓️ عدد الأيام: {days}",
                *_explain(
                    "➖ يوم — يقصّر نهاية الفترة يوماً واحداً.",
                    "➕ يوم — يمدد نهاية الفترة يوماً واحداً.",
                    "🧠 توليد المسودة الذكية — يحلل التاريخ السابق لكل صيدلية ويولد النهاري والليلي والجمعات.",
                    "🔙 رجوع — يرجع للقسم الذكي بدون إنشاء شيء.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button("➖ يوم", f"a:smart:range:{start_date.isoformat()}:{(end_date - timedelta(days=1)).isoformat()}"),
                    keyboards.button("➕ يوم", f"a:smart:range:{start_date.isoformat()}:{(end_date + timedelta(days=1)).isoformat()}"),
                ],
                [keyboards.button("🧠 توليد المسودة الذكية", f"a:smart:generate:{start_date.isoformat()}:{end_date.isoformat()}", ButtonStyle.SUCCESS)],
                [keyboards.button("⬅️ رجوع", SMART_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:range:"))
async def smart_range(callback: CallbackQuery, db: Database) -> None:
    try:
        _, _, _, start_raw, end_raw = callback.data.split(":", 4)
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "التاريخ غير صالح.", alert=True)
        return
    if end_date < start_date:
        await answer_callback(callback, "لا يمكن أن تكون نهاية الجدول قبل البداية.", alert=True)
        return
    if (end_date - start_date).days > 92:
        await answer_callback(callback, "الحد الأقصى 93 يوماً للجدول الواحد.", alert=True)
        return
    await _render_range(callback, db, start_date, end_date)


@router.callback_query(F.data.startswith("a:smart:generate:"))
async def smart_generate(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, start_raw, end_raw = callback.data.split(":", 4)
        start_date = date.fromisoformat(start_raw)
        end_date = date.fromisoformat(end_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "التاريخ غير صالح.", alert=True)
        return
    async with db.session_factory() as session:
        existing = await _latest_smart_draft(session)
        if existing:
            batch_id = existing.id
        else:
            try:
                times = await get_shift_times(session)
                rows, analysis = await generate_import_rows(
                    session,
                    start_date=start_date,
                    end_date=end_date,
                    timezone=settings.timezone,
                    times=times,
                )
                batch = await repositories.create_import_batch(
                    session,
                    source_type=SMART_SOURCE_TYPE,
                    source_name=f"جدول ذكي {_date_text(start_date)} - {_date_text(end_date)}",
                    source_file_id=None,
                    created_by=callback.from_user.id,
                    rows=rows,
                )
                batch.summary = {**batch.summary, "smart_analysis": analysis.as_dict()}
                await session.commit()
                batch_id = batch.id
            except ValueError as exc:
                await answer_callback(callback, str(exc), alert=True)
                return
    await answer_callback(callback, "تم إنشاء المسودة وتحليلها.")
    await _render_draft(callback, db, settings, batch_id)


async def _render_draft(callback: CallbackQuery, db: Database, settings: Settings, batch_id: int) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.source_type != SMART_SOURCE_TYPE:
            await answer_callback(callback, "المسودة غير موجودة.", alert=True)
            return
        analysis = await analyze_batch(session, batch, settings.timezone)
    status = "📝 مسودة غير منشورة" if batch.status == "draft" else "✅ منشور"
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"المسودة الذكية #{batch.id}",
            "راجع الجدول والتحليل وعدّل أي يوم تريده. النشر لا يحدث إلا من زر الاعتماد وبعد شاشة تأكيد ثانية.",
            stats=[
                f"📌 الحالة: {status}",
                f"📅 الفترة: {_date_text(batch.period_start)} → {_date_text(batch.period_end)}" if batch.period_start and batch.period_end else "📅 الفترة غير محددة",
                f"⚖️ تقييم العدالة: {analysis.rating}",
                f"📋 المناوبات: {analysis.total_assignments}",
                f"🔁 أيام متتالية: {analysis.consecutive_assignments}",
                f"⛔ تعارضات صلبة: {analysis.hard_errors}",
                *_explain(
                    "👁️ عرض الجدول — يعرض كل الأيام صفحة صفحة قبل النشر.",
                    "📊 تحليل الجدول — يعرض التوازن والتعارضات والجمعات بالتفصيل.",
                    "✏️ تعديل الجدول — يفتح أي يوم لتغيير النهاري أو الليلي وتثبيته.",
                    "🧠 إعادة التوزيع الذكي — يولد توزيعاً جديداً مع إبقاء الأيام المثبتة.",
                    "📄 معاينة / تصدير Word — يرسل نفس تنسيق ملف Word الرسمي بالتواريخ الجديدة.",
                    "✅ اعتماد ونشر — يفتح تأكيداً أخيراً؛ لا ينشر مباشرة.",
                    "🗑️ حذف المسودة — يحذف المسودة فقط ولا يمس الجدول المنشور.",
                ),
            ],
            warning="وجود تعارض صلب يمنع النشر حتى يتم إصلاحه.",
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button("👁️ عرض الجدول", f"a:smart:view:{batch.id}:0", ButtonStyle.PRIMARY),
                    keyboards.button("📊 تحليل الجدول", f"a:smart:analysis:{batch.id}", ButtonStyle.PRIMARY),
                ],
                [keyboards.button("✏️ تعديل الجدول", f"a:smart:edit:{batch.id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🧠 إعادة التوزيع الذكي", f"a:smart:rerollask:{batch.id}", ButtonStyle.PRIMARY)],
                [keyboards.button("📄 معاينة / تصدير Word", f"a:smart:word:{batch.id}", ButtonStyle.PRIMARY)],
                [keyboards.button("✅ اعتماد ونشر", f"a:smart:publishask:{batch.id}", ButtonStyle.SUCCESS)],
                [keyboards.button("🗑️ حذف المسودة", f"a:smart:deleteask:{batch.id}", ButtonStyle.DANGER)],
                [keyboards.button("⬅️ رجوع", SMART_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:draft:"))
async def smart_draft(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    try:
        batch_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await answer_callback(callback, "المسودة غير صالحة.", alert=True)
        return
    await _render_draft(callback, db, settings, batch_id)


@router.callback_query(F.data.startswith("a:smart:view:"))
async def smart_view(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, page_raw = callback.data.split(":", 4)
        batch_id, page = int(batch_raw), max(0, int(page_raw))
    except (ValueError, AttributeError):
        await answer_callback(callback, "الصفحة غير صالحة.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None:
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return
    days = _batch_days(batch, settings.timezone)
    pages = max(1, ceil(len(days) / PAGE_SIZE))
    page = min(page, pages - 1)
    selected = days[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    lines = [f"👁️ <b>معاينة الجدول — صفحة {page + 1}/{pages}</b>", ""]
    for duty_date, day_row, evening_row in selected:
        lines.append(f"📅 <b>{_date_text(duty_date)}</b>{' 🕌' if duty_date.weekday() == 4 else ''}")
        lines.append(f"☀️ {escape(_row_name(day_row)) if day_row else 'غير محدد'}")
        lines.append(f"🌙 {escape(_row_name(evening_row)) if evening_row else 'غير محدد'}")
        if any(bool((row.raw_data or {}).get("locked")) for row in (day_row, evening_row) if row):
            lines.append("🔒 اليوم مثبت جزئياً أو كلياً")
        lines.append("")
    lines.extend(
        [
            "🔹 ◀️/▶️ — تنقل بين صفحات الجدول.",
            "🔹 ✏️ تعديل هذه الصفحة — يفتح أيام هذه الصفحة للتعديل.",
            "🔹 🔙 رجوع — يرجع للمسودة بدون تغيير.",
        ]
    )
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:view:{batch_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:view:{batch_id}:{page + 1}"))
    rows = [nav] if nav else []
    rows.extend(
        [
            [keyboards.button("✏️ تعديل هذه الصفحة", f"a:smart:edit:{batch_id}:{page}", ButtonStyle.PRIMARY)],
            [keyboards.button("⬅️ رجوع", f"a:smart:draft:{batch_id}")],
        ]
    )
    await safe_edit(callback, "\n".join(lines), keyboards.keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:edit:"))
async def smart_edit_list(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, page_raw = callback.data.split(":", 4)
        batch_id, page = int(batch_raw), max(0, int(page_raw))
    except (ValueError, AttributeError):
        await answer_callback(callback, "الصفحة غير صالحة.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        await answer_callback(callback, "هذه المسودة غير قابلة للتعديل.", alert=True)
        return
    days = _batch_days(batch, settings.timezone)
    pages = max(1, ceil(len(days) / PAGE_SIZE))
    page = min(page, pages - 1)
    selected = days[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for duty_date, _, _ in selected:
        label = f"{'🕌 ' if duty_date.weekday() == 4 else '📅 '}{_date_text(duty_date)}"
        rows.append([keyboards.button(label, f"a:smart:day:{batch_id}:{duty_date.strftime('%Y%m%d')}", ButtonStyle.PRIMARY)])
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:edit:{batch_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:edit:{batch_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع للمسودة", f"a:smart:draft:{batch_id}")])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تعديل الجدول",
            "اختر اليوم الذي تريد مراجعته. بعد تغيير أي صيدلية يعيد البوت التحليل تلقائياً ويثبت اختيارك اليدوي.",
            stats=_explain(
                "أزرار التواريخ — تفتح النهاري والليلي لذلك اليوم.",
                "◀️/▶️ — تنقل بين بقية أيام الفترة.",
                "🔙 رجوع — يرجع للمسودة بدون تعديل.",
            ),
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


async def _render_day(callback: CallbackQuery, db: Database, settings: Settings, batch_id: int, duty_date: date) -> None:
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        await answer_callback(callback, "المسودة غير موجودة أو منشورة.", alert=True)
        return
    entries = {period: row for day, *pair in _batch_days(batch, settings.timezone) if day == duty_date for period, row in zip((DAY, EVENING), pair) if row}
    day_row = entries.get(DAY)
    evening_row = entries.get(EVENING)
    if day_row is None or evening_row is None:
        await answer_callback(callback, "بيانات هذا اليوم غير مكتملة.", alert=True)
        return
    locked = bool((day_row.raw_data or {}).get("locked")) and bool((evening_row.raw_data or {}).get("locked"))
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"تعديل {_date_text(duty_date)}",
            "يمكنك تغيير النهاري أو الليلي. البوت يمنع اختيار نفس الصيدلية للفترتين ويثبت التعديل اليدوي حتى لا تضيع تغييراتك عند إعادة التوزيع.",
            stats=[
                f"☀️ النهاري: {escape(_row_name(day_row))}",
                f"🌙 الليلي: {escape(_row_name(evening_row))}",
                f"🔒 تثبيت اليوم: {'مفعل' if locked else 'غير مفعل بالكامل'}",
                *_explain(
                    "✏️ تغيير النهاري — يعرض أنسب الصيدليات مع إحصائياتها.",
                    "✏️ تغيير الليلي — يعرض أنسب الصيدليات مع إحصائياتها.",
                    "🔒/🔓 تثبيت اليوم — يحافظ على اختيارات اليوم عند إعادة التوزيع الذكي.",
                    "🔙 رجوع — يرجع لقائمة الأيام.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("✏️ تغيير النهاري", f"a:smart:choose:{batch_id}:{day_row.id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("✏️ تغيير الليلي", f"a:smart:choose:{batch_id}:{evening_row.id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🔓 إلغاء تثبيت اليوم" if locked else "🔒 تثبيت اليوم", f"a:smart:lock:{batch_id}:{duty_date.strftime('%Y%m%d')}", ButtonStyle.SUCCESS if not locked else ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", f"a:smart:edit:{batch_id}:0")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:day:"))
async def smart_day(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, date_raw = callback.data.split(":", 4)
        batch_id = int(batch_raw)
        duty_date = date(int(date_raw[:4]), int(date_raw[4:6]), int(date_raw[6:8]))
    except (ValueError, AttributeError):
        await answer_callback(callback, "اليوم غير صالح.", alert=True)
        return
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:lock:"))
async def smart_lock_day(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, date_raw = callback.data.split(":", 4)
        batch_id = int(batch_raw)
        duty_date = date(int(date_raw[:4]), int(date_raw[4:6]), int(date_raw[6:8]))
    except (ValueError, AttributeError):
        await answer_callback(callback, "اليوم غير صالح.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft":
            await answer_callback(callback, "المسودة غير قابلة للتعديل.", alert=True)
            return
        rows = [row for day, day_row, evening_row in _batch_days(batch, settings.timezone) if day == duty_date for row in (day_row, evening_row) if row]
        make_locked = not all(bool((row.raw_data or {}).get("locked")) for row in rows)
        for row in rows:
            data = dict(row.raw_data or {})
            data["locked"] = make_locked
            row.raw_data = data
        await session.commit()
    await answer_callback(callback, "تم تحديث تثبيت اليوم.")
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:choose:"))
async def smart_choose_list(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, row_raw, page_raw = callback.data.split(":", 5)
        batch_id, row_id, page = int(batch_raw), int(row_raw), max(0, int(page_raw))
    except (ValueError, AttributeError):
        await answer_callback(callback, "الاختيار غير صالح.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        target = next((row for row in batch.rows if row.id == row_id), None) if batch else None
        if batch is None or target is None or target.start_at is None:
            await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
            return
        stats, _ = await pharmacy_year_statistics(
            session,
            year=as_local(target.start_at, settings.timezone).year,
            timezone=settings.timezone,
        )
    period = _row_period(target, settings.timezone)
    stats.sort(key=lambda item: (item["total"], item[period], item["fridays"], item["name"]))
    pages = max(1, ceil(len(stats) / STATS_PAGE_SIZE))
    page = min(page, pages - 1)
    selected = stats[page * STATS_PAGE_SIZE : (page + 1) * STATS_PAGE_SIZE]
    rows = []
    for item in selected:
        icon = "☀️" if period == DAY else "🌙"
        label = f"{item['name']} • إج {item['total']} • {icon}{item[period]} • 🕌{item['fridays']}/2"
        rows.append([keyboards.button(label, f"a:smart:pick:{batch_id}:{row_id}:{item['id']}")])
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:choose:{batch_id}:{row_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:choose:{batch_id}:{row_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    duty_date = as_local(target.start_at, settings.timezone).date()
    rows.append([keyboards.button("⬅️ رجوع لليوم", f"a:smart:day:{batch_id}:{duty_date.strftime('%Y%m%d')}")])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "اختيار صيدلية بديلة",
            "القائمة مرتبة بحيث تظهر الصيدليات الأقل مناوبات والأكثر حاجة لهذه الفترة أولاً. اختيارك اليدوي يبقى مثبتاً.",
            stats=_explain(
                "زر كل صيدلية — يعرض اسمها ثم إجمالي مناوباتها وعدد هذه الفترة وعدد جمعاتها.",
                "◀️/▶️ — يعرض بقية الصيدليات.",
                "🔙 رجوع — يلغي الخروج من القائمة بدون تغيير.",
            ),
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:pick:"))
async def smart_pick(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, row_raw, pharmacy_raw = callback.data.split(":", 5)
        batch_id, row_id, pharmacy_id = int(batch_raw), int(row_raw), int(pharmacy_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "الاختيار غير صالح.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        target = next((row for row in batch.rows if row.id == row_id), None) if batch else None
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
        if batch is None or batch.status != "draft" or target is None or target.start_at is None or pharmacy is None or pharmacy.status != "active":
            await answer_callback(callback, "تعذر حفظ هذا الاختيار.", alert=True)
            return
        duty_date = as_local(target.start_at, settings.timezone).date()
        same_day_rows = [row for day, day_row, evening_row in _batch_days(batch, settings.timezone) if day == duty_date for row in (day_row, evening_row) if row and row.id != target.id]
        if any(row.matched_pharmacy_id == pharmacy_id for row in same_day_rows):
            await answer_callback(callback, "ممنوع اختيار نفس الصيدلية للنهاري والليلي بنفس اليوم.", alert=True)
            return
        target.matched_pharmacy_id = pharmacy.id
        target.raw_pharmacy_name = pharmacy.name
        target.confidence = 100.0
        target.errors = []
        target.status = "ready"
        data = dict(target.raw_data or {})
        data["locked"] = True
        data["manual_override"] = True
        target.raw_data = data
        batch.summary = repositories.summarize_import_rows(batch.rows)
        await session.commit()
        analysis = await analyze_batch(session, batch, settings.timezone)
    await answer_callback(callback, f"تم التعديل. تقييم الجدول الآن: {analysis.rating}")
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:analysis:"))
async def smart_analysis(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        batch_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await answer_callback(callback, "المسودة غير صالحة.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None:
            await answer_callback(callback, "المسودة غير موجودة.", alert=True)
            return
        analysis = await analyze_batch(session, batch, settings.timezone)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تحليل الجدول الذكي",
            "هذا الفحص يعاد حسابه من بيانات المسودة الحالية، لذلك أي تعديل يدوي يظهر أثره هنا مباشرة.",
            stats=[
                f"⚖️ التقييم: {analysis.rating}",
                f"📋 إجمالي مناوبات المسودة: {analysis.total_assignments}",
                f"📏 فرق إجمالي المناوبات بين الأقل والأكثر: {analysis.total_spread}",
                f"☀️ فرق النهاري: {analysis.day_spread}",
                f"🌙 فرق الليلي: {analysis.evening_spread}",
                f"🔁 مناوبات في يومين متتاليين: {analysis.consecutive_assignments}",
                f"⛔ نفس الصيدلية بفترتين في اليوم: {analysis.same_day_conflicts}",
                f"🕌 خانات الجمعة في المسودة: {analysis.friday_assignments}",
                f"🕌 تجاوز حد 2/2: {analysis.friday_over_limit}",
                f"🎯 مخالفات أولوية 0/2 ثم 1/2: {analysis.friday_priority_violations}",
                *_explain(
                    "🏥 إحصائيات الصيدليات — يفتح الصورة الكاملة للسنة لكل الصيدليات.",
                    "✏️ تعديل الجدول — يصلح أي تنبيه قبل النشر.",
                    "🔙 رجوع — يرجع للمسودة.",
                ),
            ],
            warning="التعارض بنفس اليوم أو تجاوز حد الجمعات يمنع النشر. الأيام المتتالية تنبيه يحاول البوت تجنبه قدر الإمكان.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("🏥 إحصائيات الصيدليات", "a:smart:stats", ButtonStyle.PRIMARY)],
                [keyboards.button("✏️ تعديل الجدول", f"a:smart:edit:{batch_id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", f"a:smart:draft:{batch_id}")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:rerollask:"))
async def smart_reroll_ask(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إعادة التوزيع الذكي",
            "سيعيد البوت توليد كل المناوبات غير المثبتة ويترك الأيام التي ثبتها الأدمن كما هي.",
            stats=_explain(
                "🧠 تأكيد إعادة التوزيع — ينشئ توزيعاً جديداً داخل نفس المسودة، ولا ينشره.",
                "❌ إلغاء — يرجع للمسودة الحالية بدون تغيير.",
            ),
            warning="المناوبات غير المثبتة قد تتغير كلها.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("🧠 تأكيد إعادة التوزيع", f"a:smart:reroll:{batch_id}", ButtonStyle.SUCCESS)],
                [keyboards.button("❌ إلغاء", f"a:smart:draft:{batch_id}", ButtonStyle.DANGER)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:reroll:"))
async def smart_reroll(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft" or not batch.period_start or not batch.period_end:
            await answer_callback(callback, "المسودة غير قابلة لإعادة التوزيع.", alert=True)
            return
        fixed = fixed_from_batch(batch, settings.timezone)
        times = await get_shift_times(session)
        try:
            prepared, analysis = await generate_import_rows(
                session,
                start_date=batch.period_start,
                end_date=batch.period_end,
                timezone=settings.timezone,
                times=times,
                fixed=fixed,
            )
        except ValueError as exc:
            await answer_callback(callback, str(exc), alert=True)
            return
        batch.rows.clear()
        for item in prepared:
            batch.rows.append(
                ImportRow(
                    row_number=int(item["row_number"]),
                    raw_pharmacy_name=str(item["raw_pharmacy_name"]),
                    matched_pharmacy_id=item.get("matched_pharmacy_id"),
                    start_at=item.get("start_at"),
                    end_at=item.get("end_at"),
                    confidence=item.get("confidence"),
                    status=str(item.get("status", "ready")),
                    errors=list(item.get("errors", [])),
                    raw_data=dict(item.get("raw_data", {})),
                )
            )
        batch.summary = {**repositories.summarize_import_rows(batch.rows), "smart_analysis": analysis.as_dict()}
        await session.commit()
    await answer_callback(callback, "تمت إعادة التوزيع مع الحفاظ على الأيام المثبتة.")
    await _render_draft(callback, db, settings, batch_id)


@router.callback_query(F.data.startswith("a:smart:word:"))
async def smart_word(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None:
            await answer_callback(callback, "الجدول غير موجود.", alert=True)
            return
        times = await get_shift_times(session)
        shifts = draft_shift_views(batch)
    try:
        content = build_official_word_schedule(shifts, settings.timezone, times)
    except WordExportError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return
    filename = f"amuda_schedule_{batch.period_start}_{batch.period_end}.docx"
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(content, filename=filename),
            caption="📄 نسخة Word بنفس تنسيق الجدول الرسمي. راجعها قبل الاعتماد والنشر.",
        )
    await answer_callback(callback, "تم إنشاء ملف Word.")


@router.callback_query(F.data.startswith("a:smart:publishask:"))
async def smart_publish_ask(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft":
            await answer_callback(callback, "المسودة غير قابلة للنشر.", alert=True)
            return
        analysis = await analyze_batch(session, batch, settings.timezone)
    if analysis.hard_errors:
        await answer_callback(callback, "يوجد تعارض صلب. افتح التحليل وعدّل الجدول أولاً.", alert=True)
        await smart_analysis(callback, db, settings)
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تأكيد اعتماد ونشر الجدول",
            "هذه هي آخر خطوة. الجدول ما زال مسودة حتى تضغط زر التأكيد الأخضر أدناه.",
            stats=[
                f"📅 الفترة: {_date_text(batch.period_start)} → {_date_text(batch.period_end)}" if batch.period_start and batch.period_end else "",
                f"⚖️ تقييم العدالة: {analysis.rating}",
                f"🔁 أيام متتالية: {analysis.consecutive_assignments}",
                f"⛔ تعارضات صلبة: {analysis.hard_errors}",
                *_explain(
                    "✅ نعم، اعتماد ونشر — يحول المسودة إلى الجدول الرسمي الذي يراه المستخدمون.",
                    "👁️ رجوع للمراجعة — يرجع للمسودة لتشاهدها أو تعدلها مرة أخرى.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("✅ نعم، اعتماد ونشر", f"a:smart:publish:{batch_id}", ButtonStyle.SUCCESS)],
                [keyboards.button("👁️ رجوع للمراجعة", f"a:smart:draft:{batch_id}", ButtonStyle.PRIMARY)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:publish:"))
async def smart_publish(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft":
            await answer_callback(callback, "المسودة غير قابلة للنشر.", alert=True)
            return
        analysis = await analyze_batch(session, batch, settings.timezone)
        if analysis.hard_errors:
            await answer_callback(callback, "تم إيقاف النشر لأن الفحص الأخير وجد تعارضاً صلباً.", alert=True)
            return
        inserted, removed = await repositories.publish_import_batch(
            session,
            batch_id,
            admin_id=callback.from_user.id,
            replace_period=False,
        )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تم اعتماد الجدول",
            "أصبح الجدول منشوراً للمستخدمين بعد المراجعة والتأكيد النهائي.",
            stats=[
                f"✅ مناوبات منشورة: {inserted}",
                f"🗑️ مناوبات مستبدلة: {removed}",
                *_explain(
                    "📄 تصدير Word — ينزل نسخة الجدول الرسمي بعد النشر.",
                    "🧠 العودة للقسم الذكي — يرجع للصفحة الرئيسية للقسم.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📄 تصدير Word", f"a:smart:word:{batch_id}", ButtonStyle.PRIMARY)],
                [keyboards.button("🧠 العودة للقسم الذكي", SMART_HOME, ButtonStyle.PRIMARY)],
            ]
        ),
    )
    await answer_callback(callback, "تم النشر بنجاح.")


@router.callback_query(F.data.startswith("a:smart:deleteask:"))
async def smart_delete_ask(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "حذف المسودة الذكية",
            "الحذف هنا يخص المسودة غير المنشورة فقط، ولا يحذف أي جدول رسمي منشور.",
            stats=_explain(
                "🗑️ تأكيد حذف المسودة — يلغي هذه المسودة نهائياً.",
                "❌ إلغاء — يرجع للمسودة بدون حذف.",
            ),
        ),
        keyboards.keyboard(
            [
                [keyboards.button("🗑️ تأكيد حذف المسودة", f"a:smart:delete:{batch_id}", ButtonStyle.DANGER)],
                [keyboards.button("❌ إلغاء", f"a:smart:draft:{batch_id}", ButtonStyle.PRIMARY)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:delete:"))
async def smart_delete(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        deleted = await repositories.cancel_import_batch(session, batch_id, callback.from_user.id)
    await answer_callback(callback, "تم حذف المسودة." if deleted else "المسودة غير موجودة.")
    await _render_smart_home(callback, db, settings)


@router.callback_query(F.data == "a:smart:stats")
async def smart_stats(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        rows, summary = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    zero = sum(1 for item in rows if item["fridays"] == 0)
    one = sum(1 for item in rows if item["fridays"] == 1)
    two = sum(1 for item in rows if item["fridays"] >= 2)
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"إحصائيات الصيدليات — {year}",
            "هذه الإحصائيات تشمل كل المناوبات المسجلة في قاعدة البوت، وليست خاصة بالجمعات فقط.",
            stats=[
                f"🏥 الصيدليات الفعالة: {summary['pharmacies']}",
                f"📋 إجمالي المناوبات: {summary['total']}",
                f"☀️ النهارية: {summary['day']}",
                f"🌙 الليلية: {summary['evening']}",
                f"🕌 بدون جمعة 0/2: {zero} | جمعة 1/2: {one} | مكتملة 2/2: {two}",
                f"⚖️ متوسط المناوبات: {summary['average']:.1f}",
                f"📏 الفرق بين الأقل والأكثر: {summary['spread']}",
                *_explain(
                    "🏥 كل الصيدليات — يعرض لكل صيدلية الإجمالي والنهاري والليلي والجمعات وآخر مناوبة.",
                    "⚖️ عدالة التوزيع — يقارن الأقل والأكثر والمتوسط والفروقات.",
                    "🕌 إحصائية الجمعات — يقسم الصيدليات إلى 0/2 و1/2 و2/2.",
                    "📅 حسب الشهر — يعرض حجم المناوبات في كل شهر.",
                    "🔙 رجوع — يرجع للقسم الذكي.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("🏥 كل الصيدليات", "a:smart:stats:list:0", ButtonStyle.PRIMARY)],
                [keyboards.button("⚖️ عدالة التوزيع", "a:smart:stats:fair", ButtonStyle.PRIMARY)],
                [keyboards.button("🕌 إحصائية الجمعات", "a:smart:stats:friday", ButtonStyle.PRIMARY)],
                [keyboards.button("📅 حسب الشهر", "a:smart:stats:months", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", SMART_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:stats:list:"))
async def smart_stats_list(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, _ = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    pages = max(1, ceil(len(items) / STATS_PAGE_SIZE))
    page = min(page, pages - 1)
    selected = items[page * STATS_PAGE_SIZE : (page + 1) * STATS_PAGE_SIZE]
    lines = [f"🏥 <b>كل الصيدليات — {year}</b>", f"صفحة {page + 1}/{pages}", ""]
    rows = []
    for item in selected:
        lines.extend(
            [
                f"💊 <b>{escape(item['name'])}</b>",
                f"📋 {item['total']} | ☀️ {item['day']} | 🌙 {item['evening']} | 🕌 {item['fridays']}/2",
                "",
            ]
        )
        rows.append([keyboards.button(f"📊 {item['name']}", f"a:smart:stats:p:{item['id']}", ButtonStyle.PRIMARY)])
    lines.extend(
        [
            "🔹 زر اسم الصيدلية — يفتح إحصائياتها الكاملة وسجل مناوباتها.",
            "🔹 ◀️/▶️ — تنقل بين بقية الصيدليات.",
            "🔹 🔙 رجوع — يرجع للإحصائيات العامة.",
        ]
    )
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:stats:list:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:stats:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", "a:smart:stats")])
    await safe_edit(callback, "\n".join(lines), keyboards.keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:stats:p:"))
async def smart_stats_pharmacy(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, summary = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    item = next((row for row in items if row["id"] == pharmacy_id), None)
    if item is None:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    last_text = "لا توجد"
    if item["last"]:
        last_text = format_date_ar(item["last"].start_at, settings.timezone)
    next_text = "لا توجد"
    if item["next"]:
        next_text = format_date_ar(item["next"].start_at, settings.timezone)
    difference = item["total"] - summary["average"]
    balance = "متوازنة ✅" if abs(difference) <= 1 else ("أعلى من المتوسط ⚠️" if difference > 0 else "أقل من المتوسط ⚠️")
    await safe_edit(
        callback,
        texts.admin_section_text(
            item["name"],
            f"إحصائية الصيدلية الكاملة لسنة {year} من جميع الجداول المنشورة.",
            stats=[
                f"📋 إجمالي المناوبات: {item['total']}",
                f"☀️ نهاري: {item['day']}",
                f"🌙 ليلي: {item['evening']}",
                f"🕌 جمعات: {item['fridays']}/2",
                f"📅 آخر مناوبة: {last_text}",
                f"⏭️ المناوبة القادمة: {next_text}",
                f"⚖️ مقارنة بالمتوسط: {balance}",
                *_explain(
                    "📋 سجل المناوبات — يعرض تواريخ كل مناوبات هذه الصيدلية في السنة.",
                    "🕌 تواريخ الجمعات — يعرض الجمعات التي أخذتها الصيدلية.",
                    "🔙 رجوع — يرجع لقائمة الصيدليات.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📋 سجل المناوبات", f"a:smart:stats:h:{pharmacy_id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🕌 تواريخ الجمعات", f"a:smart:stats:pf:{pharmacy_id}", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", "a:smart:stats:list:0")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:stats:h:"))
async def smart_stats_history(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        _, _, _, _, pharmacy_raw, page_raw = callback.data.split(":", 5)
        pharmacy_id, page = int(pharmacy_raw), max(0, int(page_raw))
    except (ValueError, AttributeError):
        await answer_callback(callback, "السجل غير صالح.", alert=True)
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
        shifts = list(
            await session.scalars(
                select(Shift)
                .where(Shift.pharmacy_id == pharmacy_id, Shift.active.is_(True))
                .order_by(Shift.start_at)
            )
        )
    if pharmacy is None:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    shifts = [shift for shift in shifts if as_local(shift.start_at, settings.timezone).year == year]
    pages = max(1, ceil(len(shifts) / 12))
    page = min(page, pages - 1)
    selected = shifts[page * 12 : (page + 1) * 12]
    lines = [f"📋 <b>سجل {escape(pharmacy.name)} — {year}</b>", f"صفحة {page + 1}/{pages}", ""]
    for shift in selected:
        local = as_local(shift.start_at, settings.timezone)
        period = "☀️ نهاري" if local.hour < 18 else "🌙 ليلي"
        friday = " 🕌" if local.weekday() == 4 else ""
        lines.append(f"• {_date_text(local.date())} — {period}{friday}")
    lines.extend(["", "🔹 ◀️/▶️ — تنقل في السجل.", "🔹 🔙 رجوع — يرجع لإحصائية الصيدلية."])
    rows = []
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:stats:h:{pharmacy_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:stats:h:{pharmacy_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", f"a:smart:stats:p:{pharmacy_id}")])
    await safe_edit(callback, "\n".join(lines), keyboards.keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:stats:pf:"))
async def smart_stats_pharmacy_fridays(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, _ = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    item = next((row for row in items if row["id"] == pharmacy_id), None)
    if item is None:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    friday_lines = [f"🕌 {_date_text(value)}" for value in item["friday_dates"]] or ["⚪ لم تأخذ جمعة مسجلة بعد."]
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"جمعات {item['name']}",
            "تعرض التواريخ المسجلة في قاعدة البوت لهذه السنة.",
            stats=[f"🕌 الرصيد: {item['fridays']}/2", *friday_lines, *_explain("🔙 رجوع — يرجع لإحصائية الصيدلية.")],
        ),
        keyboards.simple_back(f"a:smart:stats:p:{pharmacy_id}"),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart:stats:fair")
async def smart_stats_fairness(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, summary = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    least = [item["name"] for item in items if item["total"] == summary["min_total"]][:8]
    most = [item["name"] for item in items if item["total"] == summary["max_total"]][:8]
    await safe_edit(
        callback,
        texts.admin_section_text(
            "عدالة توزيع المناوبات",
            "يقارن البوت إجمالي المناوبات بين جميع الصيدليات، ويستخدم هذه الأرقام عند إنشاء الجدول الجديد.",
            stats=[
                f"📉 أقل عدد: {summary['min_total']} — {', '.join(least) if least else '-'}",
                f"📈 أعلى عدد: {summary['max_total']} — {', '.join(most) if most else '-'}",
                f"⚖️ المتوسط: {summary['average']:.1f}",
                f"📏 الفرق: {summary['spread']}",
                *_explain(
                    "🏥 كل الصيدليات — يعرض أرقام كل صيدلية بالتفصيل.",
                    "🔙 رجوع — يرجع للإحصائيات.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("🏥 كل الصيدليات", "a:smart:stats:list:0", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", "a:smart:stats")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart:stats:friday")
async def smart_stats_friday(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, _ = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    groups = {count: [item for item in items if min(item["fridays"], 2) == count] for count in (0, 1, 2)}
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"إحصائية جمعات {year}",
            "هذه الصفحة جزء من الإحصائيات العامة. نظام التوليد يعطي 0/2 الأولوية ثم 1/2 ويستبعد 2/2 تلقائياً.",
            stats=[
                f"⚪ بدون جمعة 0/2: {len(groups[0])}",
                f"🟡 جمعة واحدة 1/2: {len(groups[1])}",
                f"🟢 مكتملة 2/2: {len(groups[2])}",
                *_explain(
                    "⚪ 0/2 — يعرض أسماء الصيدليات ذات الأولوية الأولى.",
                    "🟡 1/2 — يعرض أسماء الصيدليات ذات الأولوية الثانية.",
                    "🟢 2/2 — يعرض الصيدليات التي لا تدخل السحب التلقائي.",
                    "🔙 رجوع — يرجع للإحصائيات العامة.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("⚪ بدون جمعة 0/2", "a:smart:stats:f:0:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🟡 جمعة واحدة 1/2", "a:smart:stats:f:1:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🟢 مكتملة 2/2", "a:smart:stats:f:2:0", ButtonStyle.SUCCESS)],
                [keyboards.button("⬅️ رجوع", "a:smart:stats")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:stats:f:"))
async def smart_stats_friday_group(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        _, _, _, _, count_raw, page_raw = callback.data.split(":", 5)
        count, page = int(count_raw), max(0, int(page_raw))
    except (ValueError, AttributeError):
        await answer_callback(callback, "المجموعة غير صالحة.", alert=True)
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, _ = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    items = [item for item in items if min(item["fridays"], 2) == count]
    pages = max(1, ceil(len(items) / 10))
    page = min(page, pages - 1)
    selected = items[page * 10 : (page + 1) * 10]
    labels = {0: "⚪ 0/2", 1: "🟡 1/2", 2: "🟢 2/2"}
    lines = [f"{labels.get(count, '')} <b>صيدليات المجموعة</b>", f"صفحة {page + 1}/{pages}", ""]
    for item in selected:
        lines.append(f"• {escape(item['name'])} — 📋 {item['total']} | ☀️ {item['day']} | 🌙 {item['evening']}")
    lines.extend(["", "🔹 ◀️/▶️ — تنقل بين الأسماء.", "🔹 🔙 رجوع — يرجع لإحصائية الجمعات."])
    rows = []
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:stats:f:{count}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:stats:f:{count}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", "a:smart:stats:friday")])
    await safe_edit(callback, "\n".join(lines), keyboards.keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart:stats:months")
async def smart_stats_months(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, _ = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    month_totals = {month: sum(item["months"].get(month, 0) for item in items) for month in range(1, 13)}
    month_names = ["كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب", "أيلول", "تشرين الأول", "تشرين الثاني", "كانون الأول"]
    stats = [f"📅 {month_names[month - 1]}: {month_totals[month]} مناوبة" for month in range(1, 13)]
    stats.extend(_explain("🔙 رجوع — يرجع للإحصائيات العامة."))
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"المناوبات حسب الشهر — {year}",
            "يعرض مجموع المناوبات المسجلة في كل شهر حتى تظهر الفترات الخفيفة أو المزدحمة بسرعة.",
            stats=stats,
        ),
        keyboards.simple_back("a:smart:stats"),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:history:"))
async def smart_history(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    async with db.session_factory() as session:
        batches = await _smart_published_batches(session, limit=100)
    pages = max(1, ceil(len(batches) / 8))
    page = min(page, pages - 1)
    selected = batches[page * 8 : (page + 1) * 8]
    rows = []
    for batch in selected:
        label = f"📅 {_date_text(batch.period_start)} → {_date_text(batch.period_end)}" if batch.period_start and batch.period_end else f"جدول #{batch.id}"
        rows.append([keyboards.button(label, f"a:smart:published:{batch.id}", ButtonStyle.PRIMARY)])
    nav = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:history:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:history:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", SMART_HOME)])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "الجداول الذكية السابقة",
            "يعرض الجداول التي تم اعتمادها ونشرها من المولّد الذكي، ويمكن إعادة تنزيل Word لأي جدول.",
            stats=[f"📚 العدد: {len(batches)}", *_explain("زر كل فترة — يفتح تفاصيل الجدول وWord.", "◀️/▶️ — تنقل بين الجداول.", "🔙 رجوع — يرجع للقسم الذكي.")],
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:published:"))
async def smart_published(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.source_type != SMART_SOURCE_TYPE or batch.status != "published":
            await answer_callback(callback, "الجدول غير موجود.", alert=True)
            return
        analysis = await analyze_batch(session, batch, settings.timezone)
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"الجدول الذكي #{batch.id}",
            "هذا جدول منشور ومحفوظ في الأرشيف.",
            stats=[
                f"📅 {_date_text(batch.period_start)} → {_date_text(batch.period_end)}" if batch.period_start and batch.period_end else "",
                f"⚖️ تحليل التوزيع: {analysis.rating}",
                *_explain("📄 تصدير Word — يعيد إنشاء نفس الجدول الرسمي.", "🔙 رجوع — يرجع لأرشيف الجداول."),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📄 تصدير Word", f"a:smart:word:{batch.id}", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", "a:smart:history:0")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart:rules")
async def smart_rules(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "قواعد التوزيع الذكي",
            "المولّد لا يعمل كسحب أسماء أعمى؛ يحلل تاريخ كل صيدلية ويجرب عدة توزيعات ثم يحتفظ بالأفضل.",
            stats=[
                "⛔ ممنوع نفس الصيدلية نهاري وليلي بنفس اليوم.",
                "🔁 يتجنب إعطاء الصيدلية يومين متتاليين، ولا يرخّي هذا الشرط إلا إذا ضاقت الخيارات.",
                "⚖️ يعطي أفضلية لمن لديها إجمالي مناوبات أقل.",
                "☀️🌙 يوازن النهاري والليلي لكل صيدلية.",
                "🕌 الجمعة: 0/2 أولاً، ثم 1/2، و2/2 لا تدخل السحب التلقائي.",
                "🎲 العشوائية تدخل بين الخيارات المتقاربة فقط حتى يبقى التوزيع عادلاً وغير ثابت.",
                "🧠 يولد عدة احتمالات ويقارن التوازن والتعارضات قبل عرض المسودة.",
                "🔒 التعديل اليدوي يمكن تثبيته حتى لا يتغير عند إعادة التوزيع.",
                "✅ لا يوجد نشر تلقائي؛ دائماً معاينة ثم تحليل ثم تعديل ثم تأكيد نهائي.",
                *_explain("🔙 رجوع — يرجع للقسم الذكي."),
            ],
        ),
        keyboards.simple_back(SMART_HOME),
    )
    await answer_callback(callback)
