from __future__ import annotations

from datetime import time
from html import escape

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery

from app import keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.admin import router
from app.handlers.common import require_writer
from app.services.smart_schedule import DAY, EVENING, analyze_batch
from app.telegram_utils import answer_callback, safe_edit
from app.utils import as_local


def _period(row, timezone) -> str:
    value = str((row.raw_data or {}).get("period") or "")
    if value in {DAY, EVENING}:
        return value
    if row.start_at is None:
        return DAY
    local = as_local(row.start_at, timezone)
    return DAY if local.time().replace(tzinfo=None) < time(18, 0) else EVENING


def _row_name(row) -> str:
    if row.matched_pharmacy is not None:
        return row.matched_pharmacy.name
    return row.raw_pharmacy_name


def _other_rows_same_day(batch, target, timezone):
    duty_date = as_local(target.start_at, timezone).date()
    return [
        row
        for row in batch.rows
        if row.id != target.id
        and row.start_at is not None
        and as_local(row.start_at, timezone).date() == duty_date
    ]


def _prepare_manual_metadata(target) -> dict:
    data = dict(target.raw_data or {})
    # Old drafts may pre-date the reversible-edit metadata. Capture the current
    # generator choice once, before replacing it, so revert stays trustworthy.
    if not data.get("manual_override"):
        data.setdefault("generated_pharmacy_id", target.matched_pharmacy_id)
        data.setdefault("generated_pharmacy_name", _row_name(target))
    data["manual_override"] = True
    data["locked"] = True
    return data


@router.callback_query(F.data.startswith("a:smart:pick:"))
async def smart_pick_confirm(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    """Review a replacement pharmacy without mutating the smart draft."""
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
        return

    if any(
        row.matched_pharmacy_id == pharmacy_id
        for row in _other_rows_same_day(batch, target, settings.timezone)
    ):
        await answer_callback(
            callback,
            "ممنوع نفس الصيدلية نهاري ومسائي بنفس اليوم.",
            alert=True,
        )
        return

    slot = "☀️ النهاري" if _period(target, settings.timezone) == DAY else "🌙 المسائي"
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تأكيد تعديل الصيدلية",
            "راجع الاختيار. لم يتم تغيير الجدول حتى الآن.",
            stats=[
                f"📅 اليوم: {duty_date.strftime('%d/%m/%Y')}",
                f"🕒 المناوبة: {slot}",
                f"الحالية: {escape(_row_name(target))}",
                f"الجديدة: {escape(pharmacy.name)}",
                "📄 بعد الحفظ سيظهر نفس الاسم في خانته داخل Word.",
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
async def smart_save_pick(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    """Apply an explicitly confirmed pharmacy edit and re-run hard checks."""
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
        if any(
            row.matched_pharmacy_id == pharmacy_id
            for row in _other_rows_same_day(batch, target, settings.timezone)
        ):
            await answer_callback(
                callback,
                "ممنوع نفس الصيدلية نهاري ومسائي بنفس اليوم.",
                alert=True,
            )
            return

        target.raw_data = _prepare_manual_metadata(target)
        target.matched_pharmacy_id = pharmacy.id
        target.matched_pharmacy = pharmacy
        target.raw_pharmacy_name = pharmacy.name
        target.confidence = 100.0
        target.errors = []
        target.status = "ready"
        batch.summary = repositories.summarize_import_rows(batch.rows)

        # Flush first so analysis sees exactly the candidate state. Hard errors
        # are rejected and rolled back rather than leaving an unsafe draft edit.
        await session.flush()
        analysis = await analyze_batch(session, batch, settings.timezone)
        if analysis.same_day_conflicts or analysis.friday_over_limit:
            await session.rollback()
            await answer_callback(
                callback,
                "تم إلغاء الحفظ لأن التعديل يسبب تعارضاً أو يتجاوز حد الجمعة 2/2.",
                alert=True,
            )
            return
        await session.commit()

    await safe_edit(
        callback,
        texts.admin_section_text(
            "تم حفظ تعديل الصيدلية",
            "تم تحديث المسودة، وWord سيستخدم نفس الصيدلية في نفس المناوبة.",
            stats=[
                f"📅 اليوم: {duty_date.strftime('%d/%m/%Y')}",
                f"✅ الصيدلية: {escape(pharmacy.name)}",
                f"⚖️ حالة التوزيع: {analysis.rating}",
            ],
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button(
                        "⬅️ رجوع لليوم",
                        f"a:smart:day:{batch_id}:{duty_date.strftime('%Y%m%d')}",
                        ButtonStyle.PRIMARY,
                    )
                ]
            ]
        ),
    )
    await answer_callback(callback, "✅ تم حفظ التعديل.")
