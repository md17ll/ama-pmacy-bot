from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from app import repositories
from app.db import Database
from app.models import Admin
from app.telegram_utils import answer_callback


async def require_admin(event: CallbackQuery | Message, db: Database) -> Admin | None:
    async with db.session_factory() as session:
        admin = await repositories.get_admin(session, event.from_user.id)
    if admin:
        return admin
    if isinstance(event, CallbackQuery):
        await answer_callback(event, "هذا القسم متاح للإدارة فقط.", alert=True)
    else:
        await event.answer("هذا القسم متاح للإدارة فقط.")
    return None


async def require_writer(event: CallbackQuery | Message, db: Database) -> Admin | None:
    admin = await require_admin(event, db)
    if admin is None:
        return None
    if admin.role not in repositories.WRITE_ROLES:
        if isinstance(event, CallbackQuery):
            await answer_callback(event, "صلاحيتك للعرض فقط.", alert=True)
        else:
            await event.answer("صلاحيتك للعرض فقط.")
        return None
    return admin


async def require_owner(event: CallbackQuery | Message, db: Database) -> Admin | None:
    admin = await require_admin(event, db)
    if admin is None:
        return None
    if admin.role != "owner":
        if isinstance(event, CallbackQuery):
            await answer_callback(event, "هذه العملية للمالك فقط.", alert=True)
        else:
            await event.answer("هذه العملية للمالك فقط.")
        return None
    return admin
