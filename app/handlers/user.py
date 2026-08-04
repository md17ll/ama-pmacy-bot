from __future__ import annotations

from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, public_keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.states import UserSearchState
from app.telegram_utils import answer_callback, safe_edit, try_delete
from app.utils import format_date_ar, format_time_ar, html, local_day_bounds, utcnow


router = Router(name="user")


async def _render_home(target: CallbackQuery | Message, db: Database, settings: Settings) -> None:
    now = utcnow()
    async with db.session_factory() as session:
        admin = await repositories.is_admin(session, target.from_user.id)
        last_update = await repositories.latest_published_at(session)
    text = texts.user_home_text(now, settings.timezone, last_update)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, public_keyboards.user_home(admin))
        await answer_callback(target)
    else:
        await target.answer(text, reply_markup=public_keyboards.user_home(admin))


@router.message(CommandStart())
async def start_handler(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return
    source = command.args.strip()[:64] if command.args else None
    async with db.session_factory() as session:
        stored_user, is_new = await repositories.upsert_user(
            session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name or "",
            last_name=user.last_name,
            language_code=user.language_code,
            source=source,
        )
        await repositories.record_usage_event(
            session, user.id, "start", {"source": source or "direct", "is_new": is_new}
        )
        if is_new:
            recipients = await repositories.entry_notification_recipients(session)
            total_users = await repositories.user_count(session)
        else:
            recipients = []
            total_users = 0

    if is_new:
        now = utcnow()
        username = f"@{html(user.username)}" if user.username else "غير موجود"
        source_text = html(source) if source else "مباشر"
        notification = (
            "👤 <b>مستخدم جديد دخل إلى البوت</b>\n\n"
            f"🪪 الاسم: {html(user.full_name)}\n"
            f"🔗 المعرف: {username}\n"
            f"🆔 رقم المستخدم: <code>{user.id}</code>\n"
            f"🌐 لغة تلغرام: {html(user.language_code or 'غير معروفة')}\n"
            f"📣 المصدر: {source_text}\n"
            f"📅 التاريخ: {format_date_ar(now, settings.timezone)}\n"
            f"🕐 الوقت: {format_time_ar(now, settings.timezone)}\n\n"
            f"📊 عدد مستخدمي البوت: {total_users}"
        )
        for recipient in recipients:
            try:
                await bot.send_message(recipient, notification)
            except Exception:
                # A blocked or unavailable admin must not prevent the user from entering.
                continue

    await _render_home(message, db, settings)


@router.callback_query(F.data.in_({cb.USER_HOME, cb.USER_REFRESH, cb.ADMIN_BACK_USER}))
async def user_home_callback(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    await _render_home(callback, db, settings)


async def _show_period(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    *,
    offset_days: int,
    title: str,
    refresh_callback: str,
) -> None:
    now = utcnow()
    local_day = now.astimezone(settings.timezone).date() + timedelta(days=offset_days)
    start, end = local_day_bounds(local_day, settings.timezone)
    async with db.session_factory() as session:
        shifts = await repositories.list_shifts_between(session, start, end)
        await repositories.record_usage_event(
            session,
            callback.from_user.id,
            "view_today" if offset_days == 0 else "view_tomorrow",
        )
    await safe_edit(
        callback,
        texts.shifts_text(title, shifts, now, settings.timezone),
        public_keyboards.user_results(shifts, refresh_callback=refresh_callback),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.USER_NOW)
async def current_shifts_handler(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    now = utcnow()
    async with db.session_factory() as session:
        shifts = await repositories.current_shifts(session, now)
        await repositories.record_usage_event(session, callback.from_user.id, "view_now")
    await safe_edit(
        callback,
        texts.shifts_text("🌙 <b>الصيدليات المناوبة الآن</b>", shifts, now, settings.timezone),
        public_keyboards.user_results(shifts, refresh_callback=cb.USER_NOW),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.USER_TODAY)
async def today_shifts_handler(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    await _show_period(
        callback,
        db,
        settings,
        offset_days=0,
        title="📅 <b>صيدليات اليوم</b>",
        refresh_callback=cb.USER_TODAY,
    )


@router.callback_query(F.data == cb.USER_TOMORROW)
async def tomorrow_shifts_handler(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    await _show_period(
        callback,
        db,
        settings,
        offset_days=1,
        title="⏭ <b>صيدليات غداً</b>",
        refresh_callback=cb.USER_TOMORROW,
    )


@router.callback_query(F.data.startswith("u:pinfo:"))
async def pharmacy_info_handler(callback: CallbackQuery, db: Database) -> None:
    try:
        pharmacy_id = int((callback.data or "").rsplit(":", maxsplit=1)[-1])
    except (TypeError, ValueError):
        await answer_callback(callback, "تعذر فتح بيانات الصيدلية.", alert=True)
        return

    async with db.session_factory() as session:
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)

    if pharmacy is None:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return

    address = pharmacy.address.strip() if pharmacy.address else "العنوان غير مضاف"
    info = f"💊 {pharmacy.name}\n📍 {address}"
    await answer_callback(callback, info[:190], alert=True)


@router.callback_query(F.data == cb.USER_SEARCH)
async def search_prompt_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSearchState.waiting_query)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        "🔍 <b>البحث عن صيدلية</b>\n\nأرسل اسم الصيدلية أو جزءاً من اسمها.\nمثال: الشفاء",
        keyboards.simple_back(cb.USER_HOME),
    )
    await answer_callback(callback)


@router.message(UserSearchState.waiting_query, F.text)
async def search_result_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    query = (message.text or "").strip()
    state_data = await state.get_data()
    menu_message_id = state_data.get("menu_message_id")
    chat_id = state_data.get("chat_id") or message.chat.id
    now = utcnow()
    async with db.session_factory() as session:
        matches = await repositories.search_pharmacies(session, query, limit=5)
        if matches:
            pharmacy = matches[0]
            next_shift = await repositories.next_shift_for_pharmacy(session, pharmacy.id, now)
        else:
            pharmacy = None
            next_shift = None
        await repositories.record_usage_event(
            session,
            message.from_user.id,
            "user_search",
            {"query": query, "pharmacy_id": pharmacy.id if pharmacy else None},
        )
    if pharmacy:
        result_text = texts.pharmacy_result_text(pharmacy, next_shift, now, settings.timezone)
    else:
        result_text = (
            "🔍 <b>نتيجة البحث</b>\n\n"
            f"لم أجد صيدلية مطابقة للاسم: <b>{html(query)}</b>\n"
            "جرّب كتابة جزء أقصر من الاسم."
        )
    if menu_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_message_id,
                text=result_text,
                reply_markup=keyboards.back_user(),
            )
        except Exception:
            await message.answer(result_text, reply_markup=keyboards.back_user())
    else:
        await message.answer(result_text, reply_markup=keyboards.back_user())
    await try_delete(message)
    await state.clear()
