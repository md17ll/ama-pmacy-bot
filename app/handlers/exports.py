from __future__ import annotations

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from app import callbacks as cb, repositories
from app.config import Settings
from app.db import Database
from app.handlers.common import require_admin
from app.services.backup import build_json_backup
from app.services.excel import export_pharmacies, export_shifts
from app.services.shift_schedule_tools import get_shift_times
from app.services.word_export import WordExportError, build_official_word_schedule
from app.telegram_utils import answer_callback
from app.utils import as_local


router = Router(name="exports")


@router.callback_query(F.data == cb.ADMIN_EXPORT_PHARMACIES)
async def export_pharmacies_handler(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        pharmacies = await repositories.list_pharmacies(session, limit=10000)
    data = export_pharmacies(pharmacies)
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(data, filename="amuda_pharmacies.xlsx"),
            caption=f"🏥 تم تصدير {len(pharmacies)} صيدلية.",
        )
    await answer_callback(callback, "تم إرسال الملف.")


@router.callback_query(F.data == cb.ADMIN_EXPORT_SHIFTS)
async def export_shifts_handler(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        shifts = await repositories.list_all_shifts(session)
    data = export_shifts(shifts, settings.timezone)
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(data, filename="amuda_shifts.xlsx"),
            caption=f"📅 تم تصدير {len(shifts)} مناوبة.",
        )
    await answer_callback(callback, "تم إرسال الملف.")


@router.callback_query(F.data == cb.ADMIN_EXPORT_WORD)
async def export_word_handler(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        shifts = await repositories.list_all_shifts(session)
        times = await get_shift_times(session)
    try:
        data = build_official_word_schedule(shifts, settings.timezone, times)
    except WordExportError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return

    local_dates = sorted({as_local(shift.start_at, settings.timezone).date() for shift in shifts})
    first = local_dates[0].isoformat()
    last = local_dates[-1].isoformat()
    filename = f"amuda_pharmacy_schedule_{first}_{last}.docx"
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(data, filename=filename),
            caption=(
                "📄 جدول المناوبات بصيغة Word، بنفس نظام الملف الرسمي: "
                "التاريخ، الصيدلية النهارية، والصيدلية المسائية بمجموعتين جنب بعض."
            ),
        )
    await answer_callback(callback, "تم إرسال ملف Word.")


@router.callback_query(F.data == cb.ADMIN_EXPORT_JSON)
async def export_json_handler(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        pharmacies = await repositories.list_pharmacies(session, limit=10000)
        shifts = await repositories.list_all_shifts(session)
    data = build_json_backup(pharmacies, shifts, settings.timezone)
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(data, filename="amuda_bot_backup.json"),
            caption="📦 نسخة احتياطية من الصيدليات والمناوبات.",
        )
    await answer_callback(callback, "تم إرسال النسخة.")
