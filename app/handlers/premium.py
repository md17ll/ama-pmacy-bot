from __future__ import annotations

from aiogram import Router
from aiogram.enums import MessageEntityType
from aiogram.filters import Command
from aiogram.types import Message, MessageEntity

from app import public_keyboards, repositories
from app.db import Database
from app.handlers.common import require_owner


router = Router(name="premium")


def _custom_emoji_id(entities: list[MessageEntity] | None) -> str | None:
    for entity in entities or []:
        if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
            return entity.custom_emoji_id
    return None


def _find_custom_emoji_id(message: Message) -> str | None:
    own = _custom_emoji_id(message.entities) or _custom_emoji_id(message.caption_entities)
    if own:
        return own
    replied = message.reply_to_message
    if replied is None:
        return None
    return _custom_emoji_id(replied.entities) or _custom_emoji_id(replied.caption_entities)


@router.message(Command("setemoji"))
async def set_developer_button_emoji(message: Message, db: Database) -> None:
    if await require_owner(message, db) is None:
        return

    emoji_id = _find_custom_emoji_id(message)
    if not emoji_id:
        await message.answer(
            "✨ <b>تعيين إيموجي Premium لزر مطوّر البوت</b>\n\n"
            "1️⃣ أرسل إيموجي Premium واحداً في المحادثة.\n"
            "2️⃣ اضغط رد على رسالة الإيموجي.\n"
            "3️⃣ أرسل الأمر <code>/setemoji</code>.\n\n"
            "لا ترسله كملصق؛ لازم يكون Custom Emoji داخل رسالة نصية."
        )
        return

    async with db.session_factory() as session:
        await repositories.set_setting(session, "developer_button_emoji_id", emoji_id)

    await message.answer(
        "✅ تم حفظ إيموجي Premium لزر <b>مطوّر البوت</b>.\n"
        "سيظهر داخل القائمة الرئيسية ومعاينة المستخدم.",
        reply_markup=public_keyboards.user_home(
            is_admin=True,
            premium_emoji_id=emoji_id,
        ),
    )


@router.message(Command("removeemoji"))
async def remove_developer_button_emoji(message: Message, db: Database) -> None:
    if await require_owner(message, db) is None:
        return
    async with db.session_factory() as session:
        await repositories.set_setting(session, "developer_button_emoji_id", None)
    await message.answer(
        "✅ تم حذف إيموجي Premium، ورجع زر مطوّر البوت للإيموجي العادي 👨‍💻.",
        reply_markup=public_keyboards.user_home(is_admin=True),
    )
