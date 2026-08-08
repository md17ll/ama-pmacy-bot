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


_BATCH_ACTIONS = {
    "draft",
    "view",
    "edit",
    "day",
    "lock",
    "choose",
    "pick",
    "analysis",
    "rerollask",
    "reroll",
    "word",
    "publishask",
    "publish",
    "deleteask",
    "delete",
    "published",
}


def _batch_id_from_callback(data: str) -> tuple[bool, int | None]:
    """Return (needs_batch_validation, batch_id).

    ``None`` as the id means the callback claims to be a batch action but is
    malformed. Non-batch smart actions return ``False`` and are skipped.
    """
    parts = data.split(":")
    if len(parts) < 3 or parts[0:2] != ["a", "smart"]:
        return False, None

    action = parts[2]
    raw_id: str | None = None
    if action in _BATCH_ACTIONS:
        raw_id = parts[3] if len(parts) > 3 else None
    elif action == "advanced" and len(parts) > 3 and parts[3] == "draft":
        raw_id = parts[4] if len(parts) > 4 else None
    else:
        return False, None

    try:
        return True, int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return True, None


# regexp is intentional: the static button audit must continue validating the
# real workflow handlers instead of treating this broad guard as their handler.
@router.callback_query(F.data.regexp(r"^a:smart:"))
async def smart_batch_source_guard(callback: CallbackQuery, db: Database) -> None:
    needs_validation, batch_id = _batch_id_from_callback(callback.data or "")
    if not needs_validation:
        raise SkipHandler

    if await require_admin(callback, db) is None:
        return
    if batch_id is None or batch_id <= 0:
        await answer_callback(callback, "رقم الجدول غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if batch is None or batch.source_type != SMART_SOURCE_TYPE:
        await answer_callback(callback, "هذا الجدول لا يتبع مولّد الجداول الذكي.", alert=True)
        return

    raise SkipHandler
