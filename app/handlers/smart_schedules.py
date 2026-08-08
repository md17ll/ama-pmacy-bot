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
from app.models import ImportBatch, ImportRow, Shift
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
from app.utils import as_local, utcnow


SMART_HOME = "a:smart"
DAYS_PER_PAGE = 7
STATS_PER_PAGE = 6


def _install_admin_entry_button() -> None:
    current = keyboards.admin_home
    if getattr(current, "_smart_schedule_wrapped", False):
        return

    def wrapped_admin_home():
        markup = current()
        rows = [list(row) for row in markup.inline_keyboard]
        rows.insert(
            4 if len(rows) >= 4 else len(rows),
            [keyboards.button("🧠 مولّد الجداول الذكي", SMART_HOME, ButtonStyle.SUCCESS)],
        )
        return keyboards.keyboard(rows)

    setattr(wrapped_admin_home, "_smart_schedule_wrapped", True)
    keyboards.admin_home = wrapped_admin_home


_install_admin_entry_button()


def _help(*items: str) -> list[str]:
    return [f"🔹 {item}" for item in items]


def _date_text(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "غير محدد"


def _period(row: ImportRow, timezone) -> str:
    value = str((row.raw_data or {}).get("period") or "")
    if value in {DAY, EVENING}:
        return value
    if row.start_at is None:
        return DAY
    return DAY if as_local(row.start_at, timezone).hour < 18 else EVENING


def _name(row: ImportRow | None) -> str:
    if row is None:
        return "غير محدد"
    return row.matched_pharmacy.name if row.matched_pharmacy else row.raw_pharmacy_name


def _days(batch: ImportBatch, timezone) -> list[tuple[date, ImportRow | None, ImportRow | None]]:
    grouped: dict[date, dict[str, ImportRow]] = defaultdict(dict)
    for row in batch.rows:
        if row.start_at:
            grouped[as_local(row.start_at, timezone).date()][_period(row, timezone)] = row
    return [
        (duty_date, values.get(DAY), values.get(EVENING))
        for duty_date, values in sorted(grouped.items())
    ]


async def _latest_draft(session) -> ImportBatch | None:
    return await session.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows).selectinload(ImportRow.matched_pharmacy))
        .where(ImportBatch.source_type == SMART_SOURCE_TYPE, ImportBatch.status == "draft")
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(1)
    )


async def _published(session, limit: int = 100) -> list[ImportBatch]:
    result = await session.scalars(
        select(ImportBatch)
        .where(ImportBatch.source_type == SMART_SOURCE_TYPE, ImportBatch.status == "published")
        .order_by(ImportBatch.published_at.desc(), ImportBatch.id.desc())
        .limit(limit)
    )
    return list(result)


async def _render_home(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    async with db.session_factory() as session:
        latest = await repositories.latest_shift_end(session)
        draft = await _latest_draft(session)
        published = await _published(session)
    latest_date = as_local(latest, settings.timezone).date() if latest else None
    lines = [
        f"📋 آخر يوم منشور: {_date_text(latest_date)}",
        f"⏭️ بداية الجدول التالي: {_date_text(latest_date + timedelta(days=1))}" if latest_date else "⏭️ سيبدأ الجدول من تاريخ اليوم.",
        f"📚 الجداول الذكية المنشورة: {len(published)}",
        *_help(
            "✨ إنشاء جدول جديد — ينشئ مسودة فقط ولا ينشر تلقائياً.",
            "📊 إحصائيات الصيدليات — يعرض كل المناوبات والنهاري والليلي والجمعات والعدالة.",
            "📚 الجداول السابقة — يفتح الجداول التي اعتمدتها من المولّد.",
            "⚙️ قواعد التوزيع — يشرح كيف يحلل البوت الاختيارات.",
            "🔙 رجوع — يرجع للوحة الإدارة.",
        ),
    ]
    rows = [[keyboards.button("✨ إنشاء جدول جديد", "a:smart:new", ButtonStyle.SUCCESS)]]
    if draft:
        lines.insert(3, "🔹 📝 فتح المسودة الحالية — يكمل المراجعة والتعديل قبل النشر.")
        rows.append([keyboards.button("📝 فتح المسودة الحالية", f"a:smart:draft:{draft.id}", ButtonStyle.PRIMARY)])
    rows.extend(
        [
            [keyboards.button("📊 إحصائيات الصيدليات", "a:smart:stats", ButtonStyle.PRIMARY)],
            [keyboards.button("📚 الجداول السابقة", "a:smart:history:0", ButtonStyle.PRIMARY)],
            [keyboards.button("⚙️ قواعد التوزيع", "a:smart:rules", ButtonStyle.PRIMARY)],
            [keyboards.button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "مولّد الجداول الذكي",
            "المسار دائماً: توليد مسودة ← عرض ← تحليل ← تعديل ← Word ← تأكيد نهائي ← نشر.",
            stats=lines,
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart")
async def smart_home(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    await _render_home(callback, db, settings)


async def _render_range(callback: CallbackQuery, start_date: date, end_date: date) -> None:
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إنشاء جدول ذكي جديد",
            "راجع الفترة. التوليد ينشئ مسودة قابلة للمراجعة والتعديل ولا ينشر أي مناوبة.",
            stats=[
                f"📅 البداية: {_date_text(start_date)}",
                f"📅 النهاية: {_date_text(end_date)}",
                f"🗓️ عدد الأيام: {(end_date - start_date).days + 1}",
                *_help(
                    "➖ يوم — يقصر نهاية الفترة يوماً.",
                    "➕ يوم — يمدد نهاية الفترة يوماً.",
                    "🧠 توليد المسودة — يحلل تاريخ الصيدليات ثم يولد الجدول.",
                    "🔙 رجوع — يرجع بدون إنشاء شيء.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button("➖ يوم", f"a:smart:range:{start_date.isoformat()}:{(end_date - timedelta(days=1)).isoformat()}"),
                    keyboards.button("➕ يوم", f"a:smart:range:{start_date.isoformat()}:{(end_date + timedelta(days=1)).isoformat()}"),
                ],
                [keyboards.button("🧠 توليد المسودة", f"a:smart:generate:{start_date.isoformat()}:{end_date.isoformat()}", ButtonStyle.SUCCESS)],
                [keyboards.button("⬅️ رجوع", SMART_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart:new")
async def smart_new(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    async with db.session_factory() as session:
        draft = await _latest_draft(session)
        latest = await repositories.latest_shift_end(session)
    if draft:
        await answer_callback(callback, "يوجد مسودة ذكية حالياً. افتحها أو احذفها أولاً.", alert=True)
        await _render_draft(callback, db, settings, draft.id)
        return
    start_date, end_date = default_period(latest, settings.timezone)
    await _render_range(callback, start_date, end_date)


@router.callback_query(F.data.startswith("a:smart:range:"))
async def smart_range(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, start_raw, end_raw = callback.data.split(":", 4)
        start_date, end_date = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "التاريخ غير صالح.", alert=True)
        return
    if end_date < start_date or (end_date - start_date).days > 92:
        await answer_callback(callback, "الفترة غير صالحة. الحد الأقصى 93 يوماً.", alert=True)
        return
    await _render_range(callback, start_date, end_date)


@router.callback_query(F.data.startswith("a:smart:generate:"))
async def smart_generate(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, start_raw, end_raw = callback.data.split(":", 4)
        start_date, end_date = date.fromisoformat(start_raw), date.fromisoformat(end_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "التاريخ غير صالح.", alert=True)
        return
    async with db.session_factory() as session:
        old = await _latest_draft(session)
        if old:
            batch_id = old.id
        else:
            try:
                times = await get_shift_times(session)
                prepared, analysis = await generate_import_rows(
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
                    rows=prepared,
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
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.source_type != SMART_SOURCE_TYPE:
            await answer_callback(callback, "المسودة غير موجودة.", alert=True)
            return
        analysis = await analyze_batch(session, batch, settings.timezone)
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"المسودة الذكية #{batch.id}",
            "الجدول غير منشور. افحصه وعدله وصدّر Word، ثم استخدم الاعتماد فقط بعد أن تتأكد منه.",
            stats=[
                f"📌 الحالة: {'📝 غير منشور' if batch.status == 'draft' else '✅ منشور'}",
                f"📅 الفترة: {_date_text(batch.period_start)} → {_date_text(batch.period_end)}",
                f"⚖️ تقييم العدالة: {analysis.rating}",
                f"📋 المناوبات: {analysis.total_assignments}",
                f"🔁 يومان متتاليان: {analysis.consecutive_assignments}",
                f"⛔ تعارضات صلبة: {analysis.hard_errors}",
                *_help(
                    "👁️ عرض الجدول — يعرض الأيام والنهاري والليلي قبل النشر.",
                    "📊 تحليل الجدول — يعرض العدالة والتعارضات ونظام الجمعة.",
                    "✏️ تعديل الجدول — يغير أي صيدلية ويثبت اختيارك اليدوي.",
                    "🧠 إعادة التوزيع — يغير غير المثبت فقط.",
                    "📄 Word — يرسل نفس شكل الجدول الرسمي بالتواريخ الجديدة.",
                    "✅ اعتماد ونشر — يفتح شاشة تأكيد ثانية ولا ينشر مباشرة.",
                    "🗑️ حذف المسودة — يلغي المسودة فقط.",
                ),
            ],
            warning="النشر يتوقف تلقائياً إذا بقي تعارض صلب.",
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button("👁️ عرض الجدول", f"a:smart:view:{batch.id}:0", ButtonStyle.PRIMARY),
                    keyboards.button("📊 تحليل الجدول", f"a:smart:analysis:{batch.id}", ButtonStyle.PRIMARY),
                ],
                [keyboards.button("✏️ تعديل الجدول", f"a:smart:edit:{batch.id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🧠 إعادة التوزيع", f"a:smart:rerollask:{batch.id}", ButtonStyle.PRIMARY)],
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
    if await require_admin(callback, db) is None:
        return
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
    days = _days(batch, settings.timezone)
    pages = max(1, ceil(len(days) / DAYS_PER_PAGE))
    page = min(page, pages - 1)
    lines = [f"👁️ <b>الجدول — صفحة {page + 1}/{pages}</b>", ""]
    for duty_date, day_row, evening_row in days[page * DAYS_PER_PAGE : (page + 1) * DAYS_PER_PAGE]:
        lines.extend(
            [
                f"📅 <b>{_date_text(duty_date)}</b>{' 🕌' if duty_date.weekday() == 4 else ''}",
                f"☀️ {escape(_name(day_row))}",
                f"🌙 {escape(_name(evening_row))}",
                "",
            ]
        )
    lines.extend(_help("◀️/▶️ — تنقل بين الصفحات.", "✏️ تعديل — يفتح أيام الصفحة للتعديل.", "🔙 رجوع — يرجع للمسودة."))
    rows = []
    nav = []
    if page:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:view:{batch_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:view:{batch_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.extend(
        [
            [keyboards.button("✏️ تعديل هذه الصفحة", f"a:smart:edit:{batch_id}:{page}", ButtonStyle.PRIMARY)],
            [keyboards.button("⬅️ رجوع", f"a:smart:draft:{batch_id}")],
        ]
    )
    await safe_edit(callback, "\n".join(lines), keyboards.keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:edit:"))
async def smart_edit(callback: CallbackQuery, db: Database, settings: Settings) -> None:
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
        await answer_callback(callback, "المسودة غير قابلة للتعديل.", alert=True)
        return
    days = _days(batch, settings.timezone)
    pages = max(1, ceil(len(days) / DAYS_PER_PAGE))
    page = min(page, pages - 1)
    rows = []
    for duty_date, _, _ in days[page * DAYS_PER_PAGE : (page + 1) * DAYS_PER_PAGE]:
        rows.append([keyboards.button(f"{'🕌' if duty_date.weekday() == 4 else '📅'} {_date_text(duty_date)}", f"a:smart:day:{batch_id}:{duty_date.strftime('%Y%m%d')}", ButtonStyle.PRIMARY)])
    nav = []
    if page:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:edit:{batch_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:edit:{batch_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", f"a:smart:draft:{batch_id}")])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تعديل الجدول",
            "اختر يوماً، ثم غير النهاري أو الليلي. أي اختيار يدوي يُثبت تلقائياً.",
            stats=_help("زر التاريخ — يفتح ذلك اليوم.", "◀️/▶️ — تنقل بين الأيام.", "🔙 رجوع — يرجع للمسودة."),
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


async def _render_day(callback: CallbackQuery, db: Database, settings: Settings, batch_id: int, duty_date: date) -> None:
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return
    entry = next(((day_row, evening_row) for day, day_row, evening_row in _days(batch, settings.timezone) if day == duty_date), None)
    if not entry or not entry[0] or not entry[1]:
        await answer_callback(callback, "بيانات اليوم غير مكتملة.", alert=True)
        return
    day_row, evening_row = entry
    locked = all(bool((row.raw_data or {}).get("locked")) for row in (day_row, evening_row))
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"تعديل {_date_text(duty_date)}",
            "ممنوع نفس الصيدلية نهاري وليلي. التعديل اليدوي يبقى مثبتاً عند إعادة التوزيع.",
            stats=[
                f"☀️ النهاري: {escape(_name(day_row))}",
                f"🌙 الليلي: {escape(_name(evening_row))}",
                f"🔒 تثبيت اليوم: {'نعم' if locked else 'لا'}",
                *_help(
                    "✏️ تغيير النهاري — يعرض الصيدليات مرتبة حسب الحاجة والتوازن.",
                    "✏️ تغيير الليلي — يعرض الصيدليات مرتبة حسب الحاجة والتوازن.",
                    "🔒/🔓 تثبيت — يحافظ على اليوم أو يسمح بإعادة توليده.",
                    "🔙 رجوع — يرجع لقائمة الأيام.",
                ),
            ],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("✏️ تغيير النهاري", f"a:smart:choose:{batch_id}:{day_row.id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("✏️ تغيير الليلي", f"a:smart:choose:{batch_id}:{evening_row.id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🔓 إلغاء تثبيت اليوم" if locked else "🔒 تثبيت اليوم", f"a:smart:lock:{batch_id}:{duty_date.strftime('%Y%m%d')}", ButtonStyle.PRIMARY)],
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
        _, _, _, batch_raw, raw = callback.data.split(":", 4)
        batch_id = int(batch_raw)
        duty_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (ValueError, AttributeError):
        await answer_callback(callback, "اليوم غير صالح.", alert=True)
        return
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:lock:"))
async def smart_lock(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, raw = callback.data.split(":", 4)
        batch_id = int(batch_raw)
        duty_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (ValueError, AttributeError):
        await answer_callback(callback, "اليوم غير صالح.", alert=True)
        return
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft":
            await answer_callback(callback, "المسودة غير قابلة للتعديل.", alert=True)
            return
        day_rows = [row for day, d, e in _days(batch, settings.timezone) if day == duty_date for row in (d, e) if row]
        new_value = not all(bool((row.raw_data or {}).get("locked")) for row in day_rows)
        for row in day_rows:
            data = dict(row.raw_data or {})
            data["locked"] = new_value
            row.raw_data = data
        await session.commit()
    await answer_callback(callback, "تم تحديث تثبيت اليوم.")
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:choose:"))
async def smart_choose(callback: CallbackQuery, db: Database, settings: Settings) -> None:
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
        if target is None or target.start_at is None:
            await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
            return
        items, _ = await pharmacy_year_statistics(session, year=as_local(target.start_at, settings.timezone).year, timezone=settings.timezone)
    slot = _period(target, settings.timezone)
    items.sort(key=lambda item: (item["total"], item[slot], item["fridays"], item["name"]))
    pages = max(1, ceil(len(items) / STATS_PER_PAGE))
    page = min(page, pages - 1)
    rows = []
    for item in items[page * STATS_PER_PAGE : (page + 1) * STATS_PER_PAGE]:
        icon = "☀️" if slot == DAY else "🌙"
        rows.append([keyboards.button(f"{item['name']} • إج{item['total']} • {icon}{item[slot]} • 🕌{item['fridays']}/2", f"a:smart:pick:{batch_id}:{row_id}:{item['id']}")])
    nav = []
    if page:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:choose:{batch_id}:{row_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:choose:{batch_id}:{row_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    duty_date = as_local(target.start_at, settings.timezone).date()
    rows.append([keyboards.button("⬅️ رجوع", f"a:smart:day:{batch_id}:{duty_date.strftime('%Y%m%d')}")])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "اختيار صيدلية بديلة",
            "الأسماء مرتبة بالأقل مناوبات ثم الأقل في نوع المناوبة، مع إظهار رصيد الجمعة.",
            stats=_help("زر الصيدلية — يحفظها ويثبت التعديل.", "◀️/▶️ — يعرض بقية الصيدليات.", "🔙 رجوع — بدون تغيير."),
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
            await answer_callback(callback, "تعذر حفظ الاختيار.", alert=True)
            return
        duty_date = as_local(target.start_at, settings.timezone).date()
        paired = [row for day, d, e in _days(batch, settings.timezone) if day == duty_date for row in (d, e) if row and row.id != target.id]
        if any(row.matched_pharmacy_id == pharmacy_id for row in paired):
            await answer_callback(callback, "ممنوع نفس الصيدلية نهاري وليلي بنفس اليوم.", alert=True)
            return
        target.matched_pharmacy_id = pharmacy.id
        target.raw_pharmacy_name = pharmacy.name
        target.confidence = 100.0
        target.errors = []
        target.status = "ready"
        data = dict(target.raw_data or {})
        data.update({"locked": True, "manual_override": True})
        target.raw_data = data
        batch.summary = repositories.summarize_import_rows(batch.rows)
        await session.commit()
        analysis = await analyze_batch(session, batch, settings.timezone)
    await answer_callback(callback, f"تم التعديل. تقييم الجدول: {analysis.rating}")
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
            "تحليل الجدول",
            "الفحص يعاد حسابه من المسودة الحالية بعد كل تعديل.",
            stats=[
                f"⚖️ التقييم: {analysis.rating}",
                f"📏 فرق الإجمالي: {analysis.total_spread}",
                f"☀️ فرق النهاري: {analysis.day_spread}",
                f"🌙 فرق الليلي: {analysis.evening_spread}",
                f"🔁 يومان متتاليان: {analysis.consecutive_assignments}",
                f"⛔ نفس الصيدلية في اليوم: {analysis.same_day_conflicts}",
                f"🕌 خانات الجمعة: {analysis.friday_assignments}",
                f"🕌 تجاوز 2/2: {analysis.friday_over_limit}",
                f"🎯 مخالفة أولوية 0/2 ثم 1/2: {analysis.friday_priority_violations}",
                *_help("✏️ تعديل — يصلح أي تنبيه.", "🏥 الإحصائيات — يفتح كل الصيدليات.", "🔙 رجوع — يرجع للمسودة."),
            ],
            warning="التعارض بنفس اليوم أو تجاوز جمعتي السنة يمنع النشر.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("✏️ تعديل الجدول", f"a:smart:edit:{batch_id}:0", ButtonStyle.PRIMARY)],
                [keyboards.button("🏥 إحصائيات الصيدليات", "a:smart:stats", ButtonStyle.PRIMARY)],
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
            "إعادة التوزيع",
            "سيعاد توليد غير المثبت فقط. الأيام والتعديلات المثبتة تبقى كما هي.",
            stats=_help("🧠 تأكيد — يعيد التوليد داخل المسودة فقط.", "❌ إلغاء — يرجع بدون تغيير."),
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
        try:
            prepared, analysis = await generate_import_rows(
                session,
                start_date=batch.period_start,
                end_date=batch.period_end,
                timezone=settings.timezone,
                times=await get_shift_times(session),
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
    await answer_callback(callback, "تمت إعادة التوزيع مع الحفاظ على المثبت.")
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
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(content, filename=f"amuda_schedule_{batch.period_start}_{batch.period_end}.docx"),
            caption="📄 نفس تنسيق Word الرسمي. راجعه قبل الاعتماد والنشر.",
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
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تأكيد اعتماد ونشر",
            "هذه آخر خطوة. الجدول ما زال مسودة ولن ينشر حتى تضغط التأكيد الأخضر.",
            stats=[
                f"📅 {_date_text(batch.period_start)} → {_date_text(batch.period_end)}",
                f"⚖️ التقييم: {analysis.rating}",
                f"🔁 يومان متتاليان: {analysis.consecutive_assignments}",
                *_help("✅ نعم، اعتماد ونشر — يجعل الجدول رسمياً للمستخدمين.", "👁️ رجوع للمراجعة — يرجع بدون نشر."),
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
            await answer_callback(callback, "أوقف النشر لأن الفحص الأخير وجد تعارضاً.", alert=True)
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
            "أصبح الجدول منشوراً بعد المراجعة والتأكيد النهائي.",
            stats=[f"✅ مناوبات منشورة: {inserted}", f"🗑️ مستبدلة: {removed}", *_help("📄 Word — ينزل النسخة الرسمية.", "🧠 رجوع — يرجع للقسم الذكي.")],
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📄 تصدير Word", f"a:smart:word:{batch_id}", ButtonStyle.PRIMARY)],
                [keyboards.button("🧠 رجوع للقسم", SMART_HOME, ButtonStyle.PRIMARY)],
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
            "حذف المسودة",
            "يحذف المسودة غير المنشورة فقط ولا يمس أي جدول رسمي.",
            stats=_help("🗑️ تأكيد — يلغي المسودة.", "❌ إلغاء — يرجع بدون حذف."),
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
    await _render_home(callback, db, settings)


@router.callback_query(F.data == "a:smart:stats")
async def smart_stats(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, summary = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    zero = sum(1 for item in items if item["fridays"] == 0)
    one = sum(1 for item in items if item["fridays"] == 1)
    two = sum(1 for item in items if item["fridays"] >= 2)
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"إحصائيات الصيدليات — {year}",
            "تشمل كل المناوبات المسجلة، وليست خاصة بالجمعات فقط.",
            stats=[
                f"🏥 الصيدليات: {summary['pharmacies']}",
                f"📋 إجمالي المناوبات: {summary['total']}",
                f"☀️ النهاري: {summary['day']} | 🌙 الليلي: {summary['evening']}",
                f"🕌 0/2: {zero} | 1/2: {one} | 2/2: {two}",
                f"⚖️ المتوسط: {summary['average']:.1f} | الفرق: {summary['spread']}",
                *_help(
                    "🏥 كل الصيدليات — يعرض أرقام كل صيدلية وتفاصيلها.",
                    "⚖️ عدالة التوزيع — يقارن الأقل والأكثر والمتوسط.",
                    "🕌 الجمعات — يقسم 0/2 و1/2 و2/2.",
                    "📅 حسب الشهر — يعرض إجمالي كل شهر.",
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
    pages = max(1, ceil(len(items) / STATS_PER_PAGE))
    page = min(page, pages - 1)
    chosen = items[page * STATS_PER_PAGE : (page + 1) * STATS_PER_PAGE]
    lines = [f"🏥 <b>كل الصيدليات — {year}</b>", f"صفحة {page + 1}/{pages}", ""]
    rows = []
    for item in chosen:
        lines.extend([f"💊 <b>{escape(item['name'])}</b>", f"📋 {item['total']} | ☀️ {item['day']} | 🌙 {item['evening']} | 🕌 {item['fridays']}/2", ""])
        rows.append([keyboards.button(f"📊 {item['name']}", f"a:smart:stats:p:{item['id']}", ButtonStyle.PRIMARY)])
    lines.extend(_help("زر الصيدلية — يفتح تفاصيلها وسجلها.", "◀️/▶️ — تنقل بين الأسماء.", "🔙 رجوع — يرجع للإحصائيات."))
    nav = []
    if page:
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
    last_text = _date_text(as_local(item["last"].start_at, settings.timezone).date()) if item["last"] else "لا توجد"
    next_text = _date_text(as_local(item["next"].start_at, settings.timezone).date()) if item["next"] else "لا توجد"
    delta = item["total"] - summary["average"]
    balance = "متوازنة ✅" if abs(delta) <= 1 else ("أعلى من المتوسط ⚠️" if delta > 0 else "أقل من المتوسط ⚠️")
    await safe_edit(
        callback,
        texts.admin_section_text(
            item["name"],
            f"إحصائية كاملة لسنة {year}.",
            stats=[
                f"📋 الإجمالي: {item['total']}",
                f"☀️ نهاري: {item['day']} | 🌙 ليلي: {item['evening']}",
                f"🕌 جمعات: {item['fridays']}/2",
                f"📅 آخر مناوبة: {last_text}",
                f"⏭️ المناوبة القادمة: {next_text}",
                f"⚖️ مقارنة بالمتوسط: {balance}",
                *_help("📋 سجل المناوبات — يعرض كل تواريخها في السنة.", "🕌 تواريخ الجمعات — يعرض الجمعات المسجلة.", "🔙 رجوع — يرجع للقائمة."),
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
        shifts = list(await session.scalars(select(Shift).where(Shift.pharmacy_id == pharmacy_id, Shift.active.is_(True)).order_by(Shift.start_at)))
    if pharmacy is None:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    shifts = [shift for shift in shifts if as_local(shift.start_at, settings.timezone).year == year]
    pages = max(1, ceil(len(shifts) / 12))
    page = min(page, pages - 1)
    lines = [f"📋 <b>سجل {escape(pharmacy.name)} — {year}</b>", f"صفحة {page + 1}/{pages}", ""]
    for shift in shifts[page * 12 : (page + 1) * 12]:
        local = as_local(shift.start_at, settings.timezone)
        lines.append(f"• {_date_text(local.date())} — {'☀️ نهاري' if local.hour < 18 else '🌙 ليلي'}{' 🕌' if local.weekday() == 4 else ''}")
    lines.extend(["", *_help("◀️/▶️ — تنقل بالسجل.", "🔙 رجوع — يرجع للصيدلية.")])
    rows = []
    nav = []
    if page:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:stats:h:{pharmacy_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:stats:h:{pharmacy_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", f"a:smart:stats:p:{pharmacy_id}")])
    await safe_edit(callback, "\n".join(lines), keyboards.keyboard(rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:stats:pf:"))
async def smart_stats_friday_dates(callback: CallbackQuery, db: Database, settings: Settings) -> None:
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
    dates = [f"🕌 {_date_text(value)}" for value in item["friday_dates"]] or ["⚪ لا توجد جمعة مسجلة."]
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"جمعات {item['name']}",
            "هذه التواريخ محسوبة من المناوبات المسجلة في قاعدة البوت.",
            stats=[f"🕌 الرصيد: {item['fridays']}/2", *dates, *_help("🔙 رجوع — يرجع لإحصائية الصيدلية.")],
        ),
        keyboards.simple_back(f"a:smart:stats:p:{pharmacy_id}"),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:smart:stats:fair")
async def smart_stats_fair(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    year = as_local(utcnow(), settings.timezone).year
    async with db.session_factory() as session:
        items, summary = await pharmacy_year_statistics(session, year=year, timezone=settings.timezone)
    least = [item["name"] for item in items if item["total"] == summary["min_total"]][:6]
    most = [item["name"] for item in items if item["total"] == summary["max_total"]][:6]
    await safe_edit(
        callback,
        texts.admin_section_text(
            "عدالة التوزيع",
            "هذه الأرقام تدخل مباشرة في قرار المولّد الذكي عند إنشاء الجدول الجديد.",
            stats=[
                f"📉 الأقل: {summary['min_total']} — {', '.join(least) or '-'}",
                f"📈 الأكثر: {summary['max_total']} — {', '.join(most) or '-'}",
                f"⚖️ المتوسط: {summary['average']:.1f}",
                f"📏 الفرق: {summary['spread']}",
                *_help("🏥 كل الصيدليات — يفتح التفاصيل.", "🔙 رجوع — يرجع للإحصائيات."),
            ],
        ),
        keyboards.keyboard([[keyboards.button("🏥 كل الصيدليات", "a:smart:stats:list:0", ButtonStyle.PRIMARY)], [keyboards.button("⬅️ رجوع", "a:smart:stats")]]),
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
            f"إحصائية الجمعات — {year}",
            "المولّد يعطي 0/2 الأولوية، ثم 1/2، ويستبعد 2/2 من السحب التلقائي.",
            stats=[
                f"⚪ 0/2: {len(groups[0])}",
                f"🟡 1/2: {len(groups[1])}",
                f"🟢 2/2: {len(groups[2])}",
                *_help("⚪/🟡/🟢 — يفتح أسماء المجموعة.", "🔙 رجوع — يرجع للإحصائيات."),
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
    labels = {0: "⚪ 0/2", 1: "🟡 1/2", 2: "🟢 2/2"}
    lines = [f"{labels.get(count, '')} <b>الصيدليات</b>", f"صفحة {page + 1}/{pages}", ""]
    for item in items[page * 10 : (page + 1) * 10]:
        lines.append(f"• {escape(item['name'])} — 📋{item['total']} ☀️{item['day']} 🌙{item['evening']}")
    lines.extend(["", *_help("◀️/▶️ — تنقل بين الأسماء.", "🔙 رجوع — يرجع لإحصائية الجمعات.")])
    rows = []
    nav = []
    if page:
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
    names = ["كانون الثاني", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب", "أيلول", "تشرين الأول", "تشرين الثاني", "كانون الأول"]
    stats = [f"📅 {names[m - 1]}: {sum(item['months'].get(m, 0) for item in items)} مناوبة" for m in range(1, 13)]
    stats.extend(_help("🔙 رجوع — يرجع للإحصائيات."))
    await safe_edit(callback, texts.admin_section_text(f"المناوبات حسب الشهر — {year}", "ملخص جميع المناوبات المسجلة في كل شهر.", stats=stats), keyboards.simple_back("a:smart:stats"))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:history:"))
async def smart_history(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    async with db.session_factory() as session:
        batches = await _published(session)
    pages = max(1, ceil(len(batches) / 8))
    page = min(page, pages - 1)
    rows = []
    for batch in batches[page * 8 : (page + 1) * 8]:
        rows.append([keyboards.button(f"📅 {_date_text(batch.period_start)} → {_date_text(batch.period_end)}", f"a:smart:published:{batch.id}", ButtonStyle.PRIMARY)])
    nav = []
    if page:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:history:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:history:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", SMART_HOME)])
    await safe_edit(
        callback,
        texts.admin_section_text("الجداول السابقة", "يفتح الجداول الذكية التي تم اعتمادها ويمكن إعادة تصدير Word.", stats=[f"📚 العدد: {len(batches)}", *_help("زر الفترة — يفتح الجدول.", "◀️/▶️ — تنقل.", "🔙 رجوع — يرجع للقسم الذكي.")]),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:published:"))
async def smart_published(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.source_type != SMART_SOURCE_TYPE or batch.status != "published":
        await answer_callback(callback, "الجدول غير موجود.", alert=True)
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"الجدول الذكي #{batch.id}",
            "جدول منشور ومحفوظ في الأرشيف.",
            stats=[f"📅 {_date_text(batch.period_start)} → {_date_text(batch.period_end)}", *_help("📄 Word — يعيد إنشاء الملف الرسمي.", "🔙 رجوع — يرجع للأرشيف.")],
        ),
        keyboards.keyboard([[keyboards.button("📄 تصدير Word", f"a:smart:word:{batch.id}", ButtonStyle.PRIMARY)], [keyboards.button("⬅️ رجوع", "a:smart:history:0")]]),
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
            "البوت يجرب عدة توزيعات ويختار الأفضل بدلاً من سحب أسماء عشوائي أعمى.",
            stats=[
                "⛔ ممنوع نفس الصيدلية نهاري وليلي بنفس اليوم.",
                "🔁 يتجنب يومين متتاليين قدر الإمكان.",
                "⚖️ يعطي أفضلية للأقل مناوبات.",
                "☀️🌙 يوازن النهاري والليلي.",
                "🕌 الجمعة: 0/2 ثم 1/2، و2/2 مستبعدة تلقائياً.",
                "🎲 العشوائية فقط بين الخيارات المتقاربة.",
                "🔒 التعديل اليدوي يثبت ولا يضيع عند إعادة التوزيع.",
                "✅ لا يوجد نشر تلقائي: معاينة وتحليل وتعديل ثم تأكيد نهائي.",
                *_help("🔙 رجوع — يرجع للقسم الذكي."),
            ],
        ),
        keyboards.simple_back(SMART_HOME),
    )
    await answer_callback(callback)
