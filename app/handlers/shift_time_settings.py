from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards
from app.config import Settings
from app.db import Database
from app.handlers.common import require_writer
from app.handlers.shifts import router
from app.services.shift_schedule_tools import (
    ShiftTimes,
    bulk_update_shift_times,
    count_affected_shifts,
    get_shift_times,
    undo_bulk_time_update,
    validate_shift_times,
)
from app.states import ShiftTimeSettingsState
from app.telegram_utils import answer_callback, safe_edit, try_delete
from app.utils import format_date_ar, format_time_ar, local_day_bounds, parse_date_value, parse_time_value, utcnow


def _times_from_data(data: dict) -> ShiftTimes:
    return ShiftTimes(
        day_start=time.fromisoformat(data["day_start"]),
        day_end=time.fromisoformat(data["day_end"]),
        evening_start=time.fromisoformat(data["evening_start"]),
        evening_end=time.fromisoformat(data["evening_end"]),
    )


def _times_text(times: ShiftTimes) -> str:
    return (
        f"☀️ النهارية: {format_time_ar(times.day_start)} – {format_time_ar(times.day_end)}\n"
        f"🌙 المسائية: {format_time_ar(times.evening_start)} – {format_time_ar(times.evening_end)}"
    )


async def _edit_state_message(
    bot: Bot,
    message: Message,
    data: dict,
    text: str,
    reply_markup=None,
) -> None:
    menu_message_id = data.get("menu_message_id")
    chat_id = data.get("chat_id") or message.chat.id
    if menu_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_message_id,
                text=text,
                reply_markup=reply_markup or keyboards.simple_back(cb.ADMIN_SHIFTS),
            )
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=reply_markup or keyboards.simple_back(cb.ADMIN_SHIFTS))


@router.callback_query(F.data == cb.ADMIN_SHIFT_GLOBAL_TIMES)
async def shift_times_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    async with db.session_factory() as session:
        current = await get_shift_times(session)
    await state.set_state(ShiftTimeSettingsState.waiting_day_start)
    if callback.message:
        await state.update_data(
            menu_message_id=callback.message.message_id,
            chat_id=callback.message.chat.id,
        )
    await safe_edit(
        callback,
        (
            "🕐 <b>تعديل أوقات المناوبات العامة</b>\n\n"
            "الأوقات الحالية:\n"
            f"{_times_text(current)}\n\n"
            "أرسل وقت بداية المناوبة النهارية.\n"
            "مثال: <code>1:30 مساءً</code>"
        ),
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback)


@router.message(ShiftTimeSettingsState.waiting_day_start, F.text)
async def shift_times_day_start(message: Message, bot: Bot, state: FSMContext) -> None:
    try:
        value = parse_time_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل وقت بداية النهارية مرة أخرى.")
        return
    await state.update_data(day_start=value.isoformat(timespec="minutes"))
    await state.set_state(ShiftTimeSettingsState.waiting_day_end)
    await _edit_state_message(
        bot,
        message,
        await state.get_data(),
        (
            f"☀️ بداية النهارية: <b>{format_time_ar(value)}</b>\n\n"
            "أرسل وقت نهاية المناوبة النهارية.\n"
            "مثال: <code>5:00 مساءً</code>"
        ),
    )
    await try_delete(message)


@router.message(ShiftTimeSettingsState.waiting_day_end, F.text)
async def shift_times_day_end(message: Message, bot: Bot, state: FSMContext) -> None:
    try:
        value = parse_time_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل وقت نهاية النهارية مرة أخرى.")
        return
    await state.update_data(day_end=value.isoformat(timespec="minutes"))
    await state.set_state(ShiftTimeSettingsState.waiting_evening_start)
    await _edit_state_message(
        bot,
        message,
        await state.get_data(),
        (
            f"☀️ نهاية النهارية: <b>{format_time_ar(value)}</b>\n\n"
            "أرسل وقت بداية المناوبة المسائية.\n"
            "مثال: <code>8:30 مساءً</code>"
        ),
    )
    await try_delete(message)


@router.message(ShiftTimeSettingsState.waiting_evening_start, F.text)
async def shift_times_evening_start(message: Message, bot: Bot, state: FSMContext) -> None:
    try:
        value = parse_time_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل وقت بداية المسائية مرة أخرى.")
        return
    await state.update_data(evening_start=value.isoformat(timespec="minutes"))
    await state.set_state(ShiftTimeSettingsState.waiting_evening_end)
    await _edit_state_message(
        bot,
        message,
        await state.get_data(),
        (
            f"🌙 بداية المسائية: <b>{format_time_ar(value)}</b>\n\n"
            "أرسل وقت نهاية المناوبة المسائية.\n"
            "مثال: <code>11:30 مساءً</code> أو <code>12:00 منتصف الليل</code>"
        ),
    )
    await try_delete(message)


@router.message(ShiftTimeSettingsState.waiting_evening_end, F.text)
async def shift_times_evening_end(message: Message, bot: Bot, state: FSMContext) -> None:
    try:
        value = parse_time_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل وقت نهاية المسائية مرة أخرى.")
        return
    await state.update_data(evening_end=value.isoformat(timespec="minutes"))
    data = await state.get_data()
    try:
        times = _times_from_data(data)
        validate_shift_times(times)
    except ValueError as exc:
        await state.set_state(ShiftTimeSettingsState.waiting_day_start)
        await _edit_state_message(
            bot,
            message,
            data,
            f"⚠️ {exc}\n\nأعد إدخال الأوقات من البداية. أرسل وقت بداية المناوبة النهارية.",
        )
        await try_delete(message)
        return

    await _edit_state_message(
        bot,
        message,
        data,
        (
            "✅ <b>تم إدخال الأوقات الجديدة</b>\n\n"
            f"{_times_text(times)}\n\n"
            "اختر من أي وقت يبدأ تطبيقها على المناوبات المسجلة:"
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button("من الآن", "a:s:times:scope:today", "success"),
                    keyboards.button("من الغد", "a:s:times:scope:tomorrow"),
                ],
                [keyboards.button("📅 من تاريخ أحدده", "a:s:times:scope:custom")],
                [keyboards.button("❌ إلغاء", cb.ADMIN_SHIFTS)],
            ]
        ),
    )
    await try_delete(message)


async def _show_time_confirmation(
    target: CallbackQuery | Message,
    db: Database,
    settings: Settings,
    state: FSMContext,
    effective_at: datetime,
    *,
    bot: Bot | None = None,
) -> None:
    data = await state.get_data()
    times = _times_from_data(data)
    await state.update_data(effective_at=effective_at.isoformat())
    async with db.session_factory() as session:
        count = await count_affected_shifts(session, effective_at)
    local_effective = effective_at.astimezone(settings.timezone)
    text = (
        "⚠️ <b>تأكيد تعديل أوقات المناوبات العامة</b>\n\n"
        f"{_times_text(times)}\n\n"
        f"📅 يبدأ التطبيق: {format_date_ar(local_effective)}، {format_time_ar(local_effective)}\n"
        f"📊 المناوبات التي ستتغير: {count}\n\n"
        "ستبقى أسماء الصيدليات والتواريخ كما هي، وستتغير أوقات البداية والنهاية فقط. "
        "كما ستُستخدم هذه الأوقات عند غيابها من ملفات Word الجديدة."
    )
    markup = keyboards.keyboard(
        [
            [keyboards.button("✅ تأكيد وتطبيق", "a:s:times:confirm", "success")],
            [keyboards.button("❌ إلغاء", cb.ADMIN_SHIFTS)],
        ]
    )
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, markup)
        await answer_callback(target)
    else:
        if bot is None:
            raise RuntimeError("Bot instance is required for message confirmation")
        await _edit_state_message(bot, target, data, text, markup)
        await try_delete(target)


@router.callback_query(F.data.startswith("a:s:times:scope:"))
async def shift_times_scope_selected(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(callback, db) is None:
        return
    data = await state.get_data()
    required = {"day_start", "day_end", "evening_start", "evening_end"}
    if not required.issubset(data):
        await answer_callback(callback, "انتهت جلسة تعديل الأوقات. أعد المحاولة.", alert=True)
        return
    scope = callback.data.rsplit(":", 1)[1]
    now = utcnow()
    local_today = now.astimezone(settings.timezone).date()
    if scope == "today":
        effective_at = now
    elif scope == "tomorrow":
        effective_at, _ = local_day_bounds(local_today + timedelta(days=1), settings.timezone)
    elif scope == "custom":
        await state.set_state(ShiftTimeSettingsState.waiting_effective_date)
        await safe_edit(
            callback,
            "📅 أرسل التاريخ الذي يبدأ منه تطبيق الأوقات الجديدة.\nمثال: <code>2026-08-10</code>",
            keyboards.simple_back(cb.ADMIN_SHIFTS),
        )
        await answer_callback(callback)
        return
    else:
        await answer_callback(callback, "الخيار غير معروف.", alert=True)
        return
    await _show_time_confirmation(callback, db, settings, state, effective_at)


@router.message(ShiftTimeSettingsState.waiting_effective_date, F.text)
async def shift_times_custom_date(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    try:
        selected = parse_date_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل التاريخ مرة أخرى.")
        return
    today = utcnow().astimezone(settings.timezone).date()
    if selected < today:
        await message.answer("لا يمكن تطبيق الأوقات على تاريخ قديم. اختر اليوم أو تاريخاً لاحقاً.")
        return
    effective_at, _ = local_day_bounds(selected, settings.timezone)
    await _show_time_confirmation(message, db, settings, state, effective_at, bot=bot)


@router.callback_query(F.data == "a:s:times:confirm")
async def shift_times_apply(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(callback, db) is None:
        return
    data = await state.get_data()
    try:
        times = _times_from_data(data)
        effective_at = datetime.fromisoformat(data["effective_at"])
    except (KeyError, ValueError):
        await answer_callback(callback, "انتهت جلسة تعديل الأوقات. أعد المحاولة.", alert=True)
        return
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    try:
        async with db.session_factory() as session:
            count, audit_id = await bulk_update_shift_times(
                session,
                times=times,
                effective_at=effective_at,
                timezone=settings.timezone,
                admin_id=callback.from_user.id,
            )
    except ValueError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return
    await state.clear()
    await safe_edit(
        callback,
        (
            "✅ <b>تم تحديث أوقات المناوبات العامة</b>\n\n"
            f"{_times_text(times)}\n"
            f"📊 تم تعديل {count} مناوبة مسجلة.\n\n"
            "الأوقات الجديدة أصبحت أيضاً الإعداد الافتراضي لملفات Word التي لا تكتب الأوقات في رأس الجدول."
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button(
                        "↩️ التراجع عن تعديل الأوقات",
                        f"a:s:times:undo:{audit_id}",
                        "danger",
                    )
                ],
                [keyboards.button("⬅️ رجوع", cb.ADMIN_SHIFTS)],
            ]
        ),
    )
    await answer_callback(callback, "تم تحديث الأوقات.")


@router.callback_query(F.data.startswith("a:s:times:undo:"))
async def shift_times_undo(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    audit_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with db.session_factory() as session:
            count = await undo_bulk_time_update(
                session,
                audit_id=audit_id,
                admin_id=callback.from_user.id,
            )
    except ValueError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return
    await safe_edit(
        callback,
        f"✅ تم التراجع واستعادة أوقات {count} مناوبة وإعدادات الأوقات السابقة.",
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback, "تم التراجع.")
