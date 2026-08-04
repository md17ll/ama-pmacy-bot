from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.db import Database
from app.handlers.common import require_admin, require_owner
from app.states import AdminAddState
from app.telegram_utils import answer_callback, safe_edit, try_delete


router = Router(name="admins")


@router.callback_query(F.data == cb.ADMIN_ADMINS)
async def admin_management(callback: CallbackQuery, db: Database) -> None:
    admin = await require_admin(callback, db)
    if admin is None:
        return
    async with db.session_factory() as session:
        admins_list = await repositories.list_admins(session)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إدارة الأدمن",
            "المالك يستطيع إضافة أو إزالة الإداريين وتحديد صلاحياتهم. المحرر يدير البيانات، والمشاهد يطّلع فقط.",
            stats=[
                f"👥 عدد الإداريين: {len(admins_list)}",
                f"🔐 صلاحيتك: {admin.role}",
            ],
        ),
        keyboards.admins(admins_list, owner=admin.role == "owner"),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_ADMIN_LIST)
async def admin_list_handler(callback: CallbackQuery, db: Database) -> None:
    admin = await require_admin(callback, db)
    if admin is None:
        return
    async with db.session_factory() as session:
        admins_list = await repositories.list_admins(session)
    lines = ["👥 <b>قائمة الإداريين</b>", ""]
    for item in admins_list:
        lines.append(
            f"• <code>{item.telegram_id}</code> — {item.role} — {'✅ فعال' if item.active else '🚫 متوقف'}"
        )
    await safe_edit(
        callback,
        "\n".join(lines),
        keyboards.admin_list(admins_list, owner=admin.role == "owner"),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_ADMIN_ADD)
async def admin_add_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_owner(callback, db) is None:
        return
    await state.set_state(AdminAddState.waiting_id)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إضافة أدمن",
            "أرسل Telegram User ID للشخص. يجب أن يكون رقماً فقط، ثم اختر الصلاحية.",
            warning="لا تستخدم اسم المستخدم لأنه قابل للتغيير.",
        ),
        keyboards.simple_back(cb.ADMIN_ADMINS),
    )
    await answer_callback(callback)


@router.message(AdminAddState.waiting_id, F.text)
async def admin_add_id(message: Message, bot: Bot, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("أرسل رقم Telegram User ID فقط.")
        return
    telegram_id = int(raw)
    await state.update_data(new_admin_id=telegram_id)
    await state.set_state(AdminAddState.waiting_role)
    data = await state.get_data()
    markup = keyboards.keyboard(
        [
            [keyboards.button("🛡 أدمن", "a:m:role:admin", "primary")],
            [keyboards.button("✏️ محرر", "a:m:role:editor")],
            [keyboards.button("👁 مشاهد", "a:m:role:viewer")],
            [keyboards.button("❌ إلغاء", cb.ADMIN_ADMINS)],
        ]
    )
    text = f"🆔 المستخدم: <code>{telegram_id}</code>\n\nاختر الصلاحية:"
    await _edit_state_message(bot, message, data, text, markup)
    await try_delete(message)


@router.callback_query(F.data.startswith("a:m:role:"))
async def admin_add_role(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_owner(callback, db) is None:
        return
    data = await state.get_data()
    if "new_admin_id" not in data:
        await answer_callback(callback, "انتهت جلسة الإضافة. أعد المحاولة.", alert=True)
        return
    role = callback.data.rsplit(":", 1)[1]
    async with db.session_factory() as session:
        await repositories.add_admin(
            session,
            int(data["new_admin_id"]),
            role,
            callback.from_user.id,
        )
    await state.clear()
    await safe_edit(
        callback,
        "✅ تم إضافة الأدمن بنجاح.\n\n"
        f"🆔 <code>{data['new_admin_id']}</code>\n"
        f"🔐 الصلاحية: {role}",
        keyboards.simple_back(cb.ADMIN_ADMINS),
    )
    await answer_callback(callback, "تمت الإضافة.")


@router.callback_query(F.data.startswith("a:m:remove_ask:"))
async def admin_remove_prompt(callback: CallbackQuery, db: Database) -> None:
    if await require_owner(callback, db) is None:
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "حذف أدمن",
            f"سيتم إيقاف صلاحيات المستخدم <code>{telegram_id}</code> فوراً.",
            warning="لا يمكن حذف المالك من داخل البوت.",
        ),
        keyboards.confirm_admin_remove(telegram_id),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:m:remove:"))
async def admin_remove_confirm(callback: CallbackQuery, db: Database) -> None:
    if await require_owner(callback, db) is None:
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        success = await repositories.deactivate_admin(
            session,
            telegram_id,
            callback.from_user.id,
        )
    if not success:
        await answer_callback(callback, "تعذر حذف هذا الأدمن.", alert=True)
        return
    await safe_edit(
        callback,
        "✅ تم إيقاف صلاحيات الأدمن.",
        keyboards.simple_back(cb.ADMIN_ADMINS),
    )
    await answer_callback(callback, "تم الحذف.")


@router.callback_query(F.data == cb.ADMIN_ADMIN_TOGGLE_NOTIFY)
async def admin_notify_redirect(callback: CallbackQuery, db: Database) -> None:
    admin = await require_admin(callback, db)
    if admin is None:
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إشعارات دخول المستخدمين",
            "يمكن لكل أدمن تشغيل أو إيقاف إشعار أول دخول للمستخدمين الجدد لحسابه.",
            stats=[f"🔔 حالتك: {'مفعلة' if admin.entry_notifications else 'متوقفة'}"],
        ),
        keyboards.notifications(admin.entry_notifications),
    )
    await answer_callback(callback)


async def _edit_state_message(
    bot: Bot,
    message: Message,
    data: dict,
    text: str,
    reply_markup,
) -> None:
    message_id = data.get("menu_message_id")
    chat_id = data.get("chat_id") or message.chat.id
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=reply_markup)
