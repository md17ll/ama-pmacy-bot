from __future__ import annotations

from aiogram import F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery

from app import repositories
from app.db import Database
from app.handlers.admin import router
from app.handlers.common import require_admin
from app.services.smart_schedule import SMART_SOURCE_TYPE
from app.telegram_utils import answer_callback


@router.callback_query(F.data.startswith("a:smart:revert:"))
async def smart_revert_source_guard(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        _, _, _, batch_raw, _row_raw = (callback.data or "").split(":", 4)
        batch_id = int(batch_raw)
    except (ValueError, AttributeError):
        await answer_callback(callback, "رقم الجدول غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.source_type != SMART_SOURCE_TYPE:
        await answer_callback(callback, "هذا الجدول لا يتبع مولّد الجداول الذكي.", alert=True)
        return

    raise SkipHandler
