from __future__ import annotations

from datetime import date
from html import escape

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery

from app import keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers import smart_schedules as smart_ui
from app.handlers.admin import router
from app.handlers.common import require_writer
from app.services import smart_schedule_edit_patch as edit_patch
from app.utils import as_local


async def _render_draft(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    batch_id: int,
) -> None:
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.source_type != smart_ui.SMART_SOURCE_TYPE:
            await smart_ui.answer_callback(callback, "المسودة غير موجودة.", alert=True)
            return
        analysis = await smart_ui.analyze_batch(session, batch, settings.timezone)

    await smart_ui.safe_edit(
        callback,
        texts.admin_section_text(
            f"المسودة الذكية #{batch.id}",
            "الجدول غير منشور. أسماء النهاري والمسائي هنا هي نفسها التي ستظهر داخل قالب Word الرسمي.",
            stats=[
                f"📌 الحالة: {'📝 غير منشور' if batch.status == 'draft' else '✅ منشور'}",
                f"📅 الفترة: {smart_ui._date_text(batch.period_start)} → {smart_ui._date_text(batch.period_end)}",
                f"⚖️ تقييم العدالة: {analysis.rating}",
                f"📋 المناوبات: {analysis.total_assignments}",
                f"🔁 يومان متتاليان: {analysis.consecutive_assignments}",
                f"⛔ تعارضات صلبة: {analysis.hard_errors}",
                *smart_ui._help(
                    "👁️ عرض الجدول — يعرض نفس توزيع النهاري والمسائي قبل التصدير.",
                    "📊 تحليل الجدول — يعرض العدالة والتعارضات ونظام الجمعة.",
                    "✏️ تعديل الصيدليات — يغير صيدلية النهاري أو المسائي بعد شاشة تأكيد.",
                    "↩️ رجوع للمولّد — يظهر داخل اليوم بعد أي تعديل يدوي.",
                    "🧠 إعادة التوزيع — يغير غير المثبت فقط.",
                    "📄 معاينة Word — يملأ قالبك الرسمي بنفس أسماء المسودة الحالية.",
                    "✅ اعتماد ونشر — يفتح شاشة تأكيد ثانية ولا ينشر مباشرة.",
                ),
            ],
            warning="Word لا يولّد توزيعاً جديداً؛ هو ينسخ أسماء النهاري والمسائي من هذه المسودة نفسها.",
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button("👁️ عرض الجدول", f"a:smart:view:{batch.id}:0", ButtonStyle.PRIMARY),
                    keyboards.button("📊 تحليل الجدول", f"a:smart:analysis:{batch.id}", ButtonStyle.PRIMARY),
                ],
                [keyboards.button("✏️ تعديل الصيدليات", f"a:smart:edit:{batch.id}:0")],
                [keyboards.button("🧠 إعادة التوزيع", f"a:smart:rerollask:{batch.id}")],
                [keyboards.button("📄 معاينة / تصدير Word", f"a:smart:word:{batch.id}")],
                [keyboards.button("✅ اعتماد ونشر", f"a:smart:publishask:{batch.id}", ButtonStyle.SUCCESS)],
                [keyboards.button("🗑️ حذف المسودة", f"a:smart:deleteask:{batch.id}", ButtonStyle.DANGER)],
                [keyboards.button("⬅️ رجوع", smart_ui.SMART_HOME)],
            ]
        ),
    )
    await smart_ui.answer_callback(callback)


async def _render_day(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    batch_id: int,
    duty_date: date,
) -> None:
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        await smart_ui.answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return

    entry = next(
        (
            (day_row, evening_row)
            for day, day_row, evening_row in smart_ui._days(batch, settings.timezone)
            if day == duty_date
        ),
        None,
    )
    if not entry or not entry[0] or not entry[1]:
        await smart_ui.answer_callback(callback, "بيانات اليوم غير مكتملة.", alert=True)
        return

    day_row, evening_row = entry
    locked = all(bool((row.raw_data or {}).get("locked")) for row in (day_row, evening_row))
    day_manual = bool(
        (day_row.raw_data or {}).get("manual_override")
        and (day_row.raw_data or {}).get("generated_pharmacy_id")
    )
    evening_manual = bool(
        (evening_row.raw_data or {}).get("manual_override")
        and (evening_row.raw_data or {}).get("generated_pharmacy_id")
    )

    rows = [
        [keyboards.button("☀️ تعديل النهاري", f"a:smart:choose:{batch_id}:{day_row.id}:0")],
        [keyboards.button("🌙 تعديل المسائي", f"a:smart:choose:{batch_id}:{evening_row.id}:0")],
    ]
    if day_manual:
        rows.append(
            [keyboards.button("↩️ رجوع النهاري لاختيار المولّد", f"a:smart:revert:{batch_id}:{day_row.id}")]
        )
    if evening_manual:
        rows.append(
            [keyboards.button("↩️ رجوع المسائي لاختيار المولّد", f"a:smart:revert:{batch_id}:{evening_row.id}")]
        )
    rows.extend(
        [
            [
                keyboards.button(
                    "🔓 إلغاء تثبيت اليوم" if locked else "🔒 تثبيت اليوم",
                    f"a:smart:lock:{batch_id}:{duty_date.strftime('%Y%m%d')}",
                    ButtonStyle.PRIMARY,
                )
            ],
            [keyboards.button("📄 معاينة Word", f"a:smart:word:{batch_id}")],
            [keyboards.button("⬅️ رجوع", f"a:smart:edit:{batch_id}:0")],
        ]
    )

    await smart_ui.safe_edit(
        callback,
        texts.admin_section_text(
            f"تعديل {smart_ui._date_text(duty_date)}",
            "اختر صيدلية بديلة للنهاري أو المسائي، ثم راجع الاختيار واضغط حفظ التعديل. Word يأخذ الاسم المحفوظ نفسه.",
            stats=[
                f"☀️ النهاري: {escape(smart_ui._name(day_row))}{' ✏️' if day_manual else ''}",
                f"🌙 المسائي: {escape(smart_ui._name(evening_row))}{' ✏️' if evening_manual else ''}",
                f"🔒 تثبيت اليوم: {'نعم' if locked else 'لا'}",
                *smart_ui._help(
                    "☀️/🌙 تعديل — يختار صيدلية بديلة ثم يفتح شاشة تأكيد قبل الحفظ.",
                    "↩️ رجوع للمولّد — يعيد الصيدلية التي اختارها المولّد قبل التعديل اليدوي.",
                    "📄 معاينة Word — يصدر نفس المسودة الحالية داخل القالب الرسمي.",
                    "🔙 رجوع — يرجع لقائمة الأيام.",
                ),
            ],
        ),
        keyboards.keyboard(rows),
    )
    await smart_ui.answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:revert:"))
async def smart_revert_generated_choice(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, row_raw = (callback.data or "").split(":", 4)
        batch_id, row_id = int(batch_raw), int(row_raw)
    except (ValueError, AttributeError):
        await smart_ui.answer_callback(callback, "التعديل غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        target = next((row for row in batch.rows if row.id == row_id), None) if batch else None
        if (
            batch is None
            or batch.source_type != smart_ui.SMART_SOURCE_TYPE
            or batch.status != "draft"
            or target is None
            or target.start_at is None
        ):
            await smart_ui.answer_callback(callback, "المناوبة غير قابلة للرجوع.", alert=True)
            return

        data = dict(target.raw_data or {})
        try:
            generated_id = int(data.get("generated_pharmacy_id"))
        except (TypeError, ValueError):
            generated_id = 0
        if not data.get("manual_override") or generated_id <= 0:
            await smart_ui.answer_callback(callback, "لا يوجد تعديل يدوي محفوظ لهذه المناوبة.", alert=True)
            return

        pharmacy = await repositories.get_pharmacy(session, generated_id)
        if pharmacy is None or pharmacy.status != "active":
            await smart_ui.answer_callback(
                callback,
                "صيدلية اختيار المولّد الأصلية لم تعد فعالة، لذلك لم يتم الرجوع.",
                alert=True,
            )
            return

        duty_date = as_local(target.start_at, settings.timezone).date()
        paired = [
            row
            for day, day_row, evening_row in smart_ui._days(batch, settings.timezone)
            if day == duty_date
            for row in (day_row, evening_row)
            if row and row.id != target.id
        ]
        if any(row.matched_pharmacy_id == generated_id for row in paired):
            await smart_ui.answer_callback(
                callback,
                "لا يمكن الرجوع الآن لأن اختيار المولّد الأصلي مستخدم في المناوبة الثانية لنفس اليوم.",
                alert=True,
            )
            return

        target.matched_pharmacy_id = pharmacy.id
        target.raw_pharmacy_name = pharmacy.name
        target.confidence = 100.0
        target.errors = []
        target.status = "ready"
        data.pop("manual_override", None)
        data["generated_pharmacy_id"] = generated_id
        data["generated_pharmacy_name"] = pharmacy.name
        data["locked"] = False
        target.raw_data = data
        batch.summary = repositories.summarize_import_rows(batch.rows)
        await session.commit()
        analysis = await smart_ui.analyze_batch(session, batch, settings.timezone)

    await smart_ui.answer_callback(callback, f"تم الرجوع لاختيار المولّد. التقييم: {analysis.rating}")
    await _render_day(callback, db, settings, batch_id, duty_date)


# Existing smart handlers look these module globals up at runtime, so replacing
# the render helpers upgrades the UI without duplicating the workflow handlers.
# The edit wrappers are scoped here too: app.services.smart_schedule keeps the
# original Friday-history integration unchanged for the rest of the application.
smart_ui.generate_import_rows = edit_patch.generate_import_rows
smart_ui.fixed_from_batch = edit_patch.fixed_from_batch
smart_ui._render_draft = _render_draft
smart_ui._render_day = _render_day
