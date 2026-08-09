from __future__ import annotations

from collections import defaultdict
from datetime import date
from html import escape

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import BufferedInputFile, CallbackQuery

from app import keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.admin import router
from app.handlers.common import require_admin, require_writer
from app.models import ImportRow
from app.services.shift_schedule_tools import get_shift_times
from app.services.smart_schedule import (
    DAY,
    EVENING,
    analyze_batch,
    draft_shift_views,
    fixed_from_batch,
    generate_import_rows,
)
from app.services.smart_word_template import build_smart_template_schedule
from app.services.word_export import WordExportError
from app.telegram_utils import answer_callback, safe_edit
from app.utils import as_local


_MANUAL_KEYS = {
    "manual_override",
    "generated_pharmacy_id",
    "generated_pharmacy_name",
    "generated_locked",
}


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


def _days(batch, timezone) -> list[tuple[date, ImportRow | None, ImportRow | None]]:
    grouped: dict[date, dict[str, ImportRow]] = defaultdict(dict)
    for row in batch.rows:
        if row.start_at:
            grouped[as_local(row.start_at, timezone).date()][_period(row, timezone)] = row
    return [
        (duty_date, values.get(DAY), values.get(EVENING))
        for duty_date, values in sorted(grouped.items())
    ]


def _date_text(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _manual_choice_data(row: ImportRow) -> dict:
    data = dict(row.raw_data or {})
    if not data.get("manual_override"):
        data["generated_pharmacy_id"] = row.matched_pharmacy_id
        data["generated_pharmacy_name"] = _name(row)
        data["generated_locked"] = bool(data.get("locked"))
    data["manual_override"] = True
    data["locked"] = True
    return data


def _manual_metadata(row: ImportRow) -> dict | None:
    data = dict(row.raw_data or {})
    if not data.get("manual_override") or not data.get("locked"):
        return None
    return {key: data[key] for key in _MANUAL_KEYS if key in data}


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
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return

    entry = next(
        (
            (day_row, evening_row)
            for day, day_row, evening_row in _days(batch, settings.timezone)
            if day == duty_date
        ),
        None,
    )
    if not entry or not entry[0] or not entry[1]:
        await answer_callback(callback, "بيانات اليوم غير مكتملة.", alert=True)
        return

    day_row, evening_row = entry
    day_manual = bool((day_row.raw_data or {}).get("manual_override"))
    evening_manual = bool((evening_row.raw_data or {}).get("manual_override"))
    locked = all(bool((row.raw_data or {}).get("locked")) for row in (day_row, evening_row))

    rows = [
        [keyboards.button("☀️ تعديل النهاري", f"a:smart:choose:{batch_id}:{day_row.id}:0", ButtonStyle.PRIMARY)],
    ]
    if day_manual:
        rows.append([keyboards.button("↩️ إلغاء تعديل النهاري", f"a:smart:restore:{batch_id}:{day_row.id}")])
    rows.append(
        [keyboards.button("🌙 تعديل المسائي", f"a:smart:choose:{batch_id}:{evening_row.id}:0", ButtonStyle.PRIMARY)]
    )
    if evening_manual:
        rows.append([keyboards.button("↩️ إلغاء تعديل المسائي", f"a:smart:restore:{batch_id}:{evening_row.id}")])
    rows.extend(
        [
            [
                keyboards.button(
                    "🔓 إلغاء تثبيت اليوم" if locked else "🔒 تثبيت اليوم",
                    f"a:smart:lock:{batch_id}:{duty_date.strftime('%Y%m%d')}",
                    ButtonStyle.PRIMARY,
                )
            ],
            [keyboards.button("⬅️ رجوع لقائمة الأيام", f"a:smart:edit:{batch_id}:0")],
        ]
    )

    await safe_edit(
        callback,
        texts.admin_section_text(
            f"تعديل صيدليات {_date_text(duty_date)}",
            "غيّر اسم الصيدلية في النهاري أو المسائي. لن يُحفظ أي اختيار جديد قبل ضغط زر الحفظ.",
            stats=[
                f"☀️ النهاري: {escape(_name(day_row))}{' ✏️' if day_manual else ''}",
                f"🌙 المسائي: {escape(_name(evening_row))}{' ✏️' if evening_manual else ''}",
                f"🔒 تثبيت اليوم: {'نعم' if locked else 'لا'}",
                "📄 ملف Word يأخذ هذين الاسمين من نفس المسودة بدون توزيع منفصل.",
            ],
            warning="ممنوع وضع نفس الصيدلية نهاراً ومساءً في اليوم نفسه.",
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:day:"))
async def smart_day_template_editor(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, raw = (callback.data or "").split(":", 4)
        batch_id = int(batch_raw)
        duty_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (ValueError, AttributeError):
        await answer_callback(callback, "اليوم غير صالح.", alert=True)
        return
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:pick:"))
async def smart_pick_review(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """Intercept the old one-click save and require an explicit save confirmation."""
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, row_raw, pharmacy_raw = (callback.data or "").split(":", 5)
        batch_id, row_id, pharmacy_id = int(batch_raw), int(row_raw), int(pharmacy_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "الاختيار غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        target = next((row for row in batch.rows if row.id == row_id), None) if batch else None
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
    if (
        batch is None
        or batch.status != "draft"
        or target is None
        or target.start_at is None
        or pharmacy is None
        or pharmacy.status != "active"
        or pharmacy.deleted_at is not None
    ):
        await answer_callback(callback, "تعذر مراجعة الاختيار.", alert=True)
        return

    duty_date = as_local(target.start_at, settings.timezone).date()
    if target.matched_pharmacy_id == pharmacy_id:
        await answer_callback(callback, "هذه الصيدلية موجودة في المناوبة أساساً.", alert=True)
        await _render_day(callback, db, settings, batch_id, duty_date)
        return

    paired = [
        row
        for day, day_row, evening_row in _days(batch, settings.timezone)
        if day == duty_date
        for row in (day_row, evening_row)
        if row and row.id != target.id
    ]
    if any(row.matched_pharmacy_id == pharmacy_id for row in paired):
        await answer_callback(callback, "ممنوع نفس الصيدلية نهاري ومسائي بنفس اليوم.", alert=True)
        return

    slot = "☀️ النهاري" if _period(target, settings.timezone) == DAY else "🌙 المسائي"
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تأكيد تعديل الصيدلية",
            "هذا التغيير لم يُحفظ بعد.",
            stats=[
                f"📅 اليوم: {_date_text(duty_date)}",
                f"🕒 المناوبة: {slot}",
                f"الحالية: {escape(_name(target))}",
                f"الجديدة: {escape(pharmacy.name)}",
                "📄 بعد الحفظ سيظهر نفس الاسم في ملف Word.",
            ],
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button(
                        "✅ حفظ التعديل",
                        f"a:smart:savepick:{batch_id}:{row_id}:{pharmacy_id}",
                        ButtonStyle.SUCCESS,
                    )
                ],
                [
                    keyboards.button(
                        "❌ إلغاء",
                        f"a:smart:day:{batch_id}:{duty_date.strftime('%Y%m%d')}",
                        ButtonStyle.DANGER,
                    )
                ],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:smart:savepick:"))
async def smart_save_pick(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, row_raw, pharmacy_raw = (callback.data or "").split(":", 5)
        batch_id, row_id, pharmacy_id = int(batch_raw), int(row_raw), int(pharmacy_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "الاختيار غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        target = next((row for row in batch.rows if row.id == row_id), None) if batch else None
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
        if (
            batch is None
            or batch.status != "draft"
            or target is None
            or target.start_at is None
            or pharmacy is None
            or pharmacy.status != "active"
            or pharmacy.deleted_at is not None
        ):
            await answer_callback(callback, "تعذر حفظ الاختيار.", alert=True)
            return

        duty_date = as_local(target.start_at, settings.timezone).date()
        paired = [
            row
            for day, day_row, evening_row in _days(batch, settings.timezone)
            if day == duty_date
            for row in (day_row, evening_row)
            if row and row.id != target.id
        ]
        if any(row.matched_pharmacy_id == pharmacy_id for row in paired):
            await answer_callback(callback, "ممنوع نفس الصيدلية نهاري ومسائي بنفس اليوم.", alert=True)
            return

        target.raw_data = _manual_choice_data(target)
        target.matched_pharmacy_id = pharmacy.id
        target.matched_pharmacy = pharmacy
        target.raw_pharmacy_name = pharmacy.name
        target.confidence = 100.0
        target.errors = []
        target.status = "ready"
        batch.summary = repositories.summarize_import_rows(batch.rows)
        await session.flush()
        analysis = await analyze_batch(session, batch, settings.timezone)
        if analysis.same_day_conflicts or analysis.friday_over_limit:
            await session.rollback()
            await answer_callback(
                callback,
                "تم إلغاء الحفظ لأن هذا التعديل يسبب تعارضاً أو يتجاوز حد الجمعة 2/2.",
                alert=True,
            )
            return
        await session.commit()

    await answer_callback(callback, f"✅ تم حفظ التعديل. حالة التوزيع: {analysis.rating}")
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:restore:"))
async def smart_restore_generated_choice(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, row_raw = (callback.data or "").split(":", 4)
        batch_id, row_id = int(batch_raw), int(row_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "التعديل غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        target = next((row for row in batch.rows if row.id == row_id), None) if batch else None
        if batch is None or batch.status != "draft" or target is None or target.start_at is None:
            await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
            return
        data = dict(target.raw_data or {})
        generated_id = data.get("generated_pharmacy_id")
        if not data.get("manual_override") or not isinstance(generated_id, int):
            await answer_callback(callback, "لا يوجد تعديل يدوي لإلغائه.", alert=True)
            return

        pharmacy = await repositories.get_pharmacy(session, generated_id)
        if pharmacy is None or pharmacy.status != "active" or pharmacy.deleted_at is not None:
            await answer_callback(callback, "تعذر الرجوع لأن صيدلية المولّد الأصلية لم تعد فعالة.", alert=True)
            return

        duty_date = as_local(target.start_at, settings.timezone).date()
        paired = [
            row
            for day, day_row, evening_row in _days(batch, settings.timezone)
            if day == duty_date
            for row in (day_row, evening_row)
            if row and row.id != target.id
        ]
        if any(row.matched_pharmacy_id == generated_id for row in paired):
            await answer_callback(callback, "تعذر الرجوع لأن ذلك سيكرر نفس الصيدلية في اليوم.", alert=True)
            return

        original_locked = bool(data.get("generated_locked", False))
        target.matched_pharmacy_id = pharmacy.id
        target.matched_pharmacy = pharmacy
        target.raw_pharmacy_name = pharmacy.name
        for key in _MANUAL_KEYS:
            data.pop(key, None)
        data["locked"] = original_locked
        target.raw_data = data
        target.confidence = 100.0
        target.errors = []
        target.status = "ready"
        batch.summary = repositories.summarize_import_rows(batch.rows)
        await session.flush()
        analysis = await analyze_batch(session, batch, settings.timezone)
        await session.commit()

    await answer_callback(callback, f"↩️ رجعت المناوبة لاختيار المولّد. الحالة: {analysis.rating}")
    await _render_day(callback, db, settings, batch_id, duty_date)


@router.callback_query(F.data.startswith("a:smart:reroll:"))
async def smart_reroll_preserve_manual(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    """Keep confirmed manual choices and their generator baseline during reroll."""
    if await require_writer(callback, db) is None:
        return
    try:
        batch_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await answer_callback(callback, "المسودة غير صالحة.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft" or not batch.period_start or not batch.period_end:
            await answer_callback(callback, "المسودة غير قابلة لإعادة التوزيع.", alert=True)
            return

        manual_by_slot: dict[tuple[date, str], dict] = {}
        for row in batch.rows:
            if row.start_at is None:
                continue
            metadata = _manual_metadata(row)
            if metadata:
                manual_by_slot[(as_local(row.start_at, settings.timezone).date(), _period(row, settings.timezone))] = metadata

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
            raw_data = dict(item.get("raw_data", {}))
            start_at = item.get("start_at")
            if start_at is not None:
                slot = (as_local(start_at, settings.timezone).date(), str(raw_data.get("period") or ""))
                metadata = manual_by_slot.get(slot)
                if metadata:
                    raw_data.update(metadata)
                    raw_data["locked"] = True
            batch.rows.append(
                ImportRow(
                    row_number=int(item["row_number"]),
                    raw_pharmacy_name=str(item["raw_pharmacy_name"]),
                    matched_pharmacy_id=item.get("matched_pharmacy_id"),
                    start_at=start_at,
                    end_at=item.get("end_at"),
                    confidence=item.get("confidence"),
                    status=str(item.get("status", "ready")),
                    errors=list(item.get("errors", [])),
                    raw_data=raw_data,
                )
            )
        batch.summary = {**repositories.summarize_import_rows(batch.rows), "smart_analysis": analysis.as_dict()}
        await session.commit()

    await safe_edit(
        callback,
        texts.admin_section_text(
            "تمت إعادة توزيع الجدول",
            "تم تحديث الأجزاء غير المثبتة، وبقيت تعديلات الصيدليات المحفوظة كما هي.",
            stats=[f"⚖️ حالة التوزيع: {analysis.rating}"],
        ),
        keyboards.keyboard(
            [[keyboards.button("⬅️ رجوع للمسودة", f"a:smart:draft:{batch_id}", ButtonStyle.PRIMARY)]]
        ),
    )
    await answer_callback(callback, "✅ تمت إعادة التوزيع.")


@router.callback_query(F.data.startswith("a:smart:word:"))
async def smart_word_from_official_template(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        batch_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await answer_callback(callback, "الجدول غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.period_start is None or batch.period_end is None:
            await answer_callback(callback, "الجدول غير موجود.", alert=True)
            return
        shifts = draft_shift_views(batch)

    try:
        content = build_smart_template_schedule(
            shifts,
            settings.timezone,
            period_start=batch.period_start,
            period_end=batch.period_end,
        )
    except WordExportError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return

    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(
                content,
                filename=f"amuda_schedule_{batch.period_start}_{batch.period_end}.docx",
            ),
            caption=(
                "📄 معاينة Word من القالب الرسمي. أسماء النهاري والمسائي مأخوذة حرفياً من نفس المسودة الحالية."
            ),
        )
    await answer_callback(callback, "✅ تم إنشاء معاينة Word من القالب الرسمي.")
