from __future__ import annotations

from datetime import time

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.common import require_writer
from app.handlers.shifts import router
from app.states import ShiftSwapState
from app.telegram_utils import answer_callback, safe_edit, try_delete
from app.utils import as_local, format_date_ar, format_time_ar, html, local_day_bounds, parse_date_value


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


@router.callback_query(F.data == cb.ADMIN_SHIFT_SWAP)
async def shift_swap_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(ShiftSwapState.waiting_date)
    if callback.message:
        await state.update_data(
            menu_message_id=callback.message.message_id,
            chat_id=callback.message.chat.id,
        )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تبديل الصيدلية المناوبة",
            "أرسل تاريخ المناوبة. سيعرض البوت مناوبات ذلك اليوم، ثم تختار الصيدلية البديلة.",
            warning="لن يتغير التاريخ أو وقت البداية أو النهاية؛ سيتغير اسم الصيدلية فقط.",
        ),
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback)


@router.message(ShiftSwapState.waiting_date, F.text)
async def shift_swap_date_received(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    try:
        duty_date = parse_date_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل التاريخ مرة أخرى.")
        return
    start_at, end_at = local_day_bounds(duty_date, settings.timezone)
    async with db.session_factory() as session:
        shifts = await repositories.list_shifts_between(session, start_at, end_at, limit=100)
    data = await state.get_data()
    if not shifts:
        await _edit_state_message(
            bot,
            message,
            data,
            f"⚪ لا توجد مناوبات مسجلة بتاريخ {format_date_ar(duty_date)}.",
        )
        await try_delete(message)
        await state.clear()
        return

    rows = []
    for shift in shifts:
        local_start = as_local(shift.start_at, settings.timezone)
        period = "☀️ نهارية" if local_start.time().replace(tzinfo=None) < time(18, 0) else "🌙 مسائية"
        rows.append(
            [keyboards.button(f"{period} — {shift.pharmacy.name}", f"a:s:swap:shift:{shift.id}")]
        )
    rows.append([keyboards.button("⬅️ رجوع", cb.ADMIN_SHIFTS)])
    await _edit_state_message(
        bot,
        message,
        data,
        (
            "🔁 <b>اختر المناوبة المراد تبديل صيدليتها</b>\n\n"
            f"📅 {format_date_ar(duty_date)}\n"
            "لن يتم تغيير التاريخ أو الوقت."
        ),
        keyboards.keyboard(rows),
    )
    await try_delete(message)
    await state.clear()


@router.callback_query(F.data.startswith("a:s:swap:shift:"))
async def shift_swap_choose_replacement(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_writer(callback, db) is None:
        return
    shift_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        shift = await repositories.get_shift(session, shift_id)
        pharmacies = await repositories.list_pharmacies(
            session,
            include_inactive=False,
            limit=1000,
        )
    if shift is None:
        await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
        return
    pharmacies = [item for item in pharmacies if item.id != shift.pharmacy_id]
    if not pharmacies:
        await answer_callback(callback, "لا توجد صيدلية فعالة أخرى للاختيار.", alert=True)
        return

    local_start = as_local(shift.start_at, settings.timezone)
    local_end = as_local(shift.end_at, settings.timezone)
    rows = [
        [keyboards.button(f"💊 {pharmacy.name}", f"a:s:swap:pick:{shift.id}:{pharmacy.id}")]
        for pharmacy in pharmacies[:100]
    ]
    rows.append([keyboards.button("⬅️ رجوع", cb.ADMIN_SHIFTS)])
    await safe_edit(
        callback,
        (
            "🏥 <b>اختر الصيدلية البديلة</b>\n\n"
            f"الصيدلية الحالية: <b>{html(shift.pharmacy.name)}</b>\n"
            f"📅 {format_date_ar(local_start)}\n"
            f"🕐 {format_time_ar(local_start)} – {format_time_ar(local_end)}\n\n"
            "اختر الصيدلية التي ستستلم هذه المناوبة:"
        ),
        keyboards.keyboard(rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:s:swap:pick:"))
async def shift_swap_confirm_prompt(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_writer(callback, db) is None:
        return
    parts = callback.data.split(":")
    shift_id = int(parts[4])
    pharmacy_id = int(parts[5])
    async with db.session_factory() as session:
        shift = await repositories.get_shift(session, shift_id)
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
    if shift is None or pharmacy is None or pharmacy.status != "active":
        await answer_callback(callback, "المناوبة أو الصيدلية غير موجودة.", alert=True)
        return
    if shift.pharmacy_id == pharmacy.id:
        await answer_callback(callback, "هذه الصيدلية هي صاحبة المناوبة أصلاً.", alert=True)
        return

    local_start = as_local(shift.start_at, settings.timezone)
    local_end = as_local(shift.end_at, settings.timezone)
    await safe_edit(
        callback,
        (
            "⚠️ <b>تأكيد تبديل المناوبة</b>\n\n"
            f"من: <b>{html(shift.pharmacy.name)}</b>\n"
            f"إلى: <b>{html(pharmacy.name)}</b>\n\n"
            f"📅 {format_date_ar(local_start)}\n"
            f"🕐 {format_time_ar(local_start)} – {format_time_ar(local_end)}\n\n"
            "سيتم تغيير الصيدلية فقط، ولن يتغير التاريخ أو الوقت."
        ),
        keyboards.keyboard(
            [
                [
                    keyboards.button(
                        "✅ تأكيد التبديل",
                        f"a:s:swap:confirm:{shift.id}:{pharmacy.id}",
                        "success",
                    )
                ],
                [keyboards.button("⬅️ رجوع", f"a:s:swap:shift:{shift.id}")],
                [keyboards.button("❌ إلغاء", cb.ADMIN_SHIFTS)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:s:swap:confirm:"))
async def shift_swap_apply(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
) -> None:
    if await require_writer(callback, db) is None:
        return
    parts = callback.data.split(":")
    shift_id = int(parts[4])
    pharmacy_id = int(parts[5])
    try:
        async with db.session_factory() as session:
            old_shift = await repositories.get_shift(session, shift_id)
            pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
            if old_shift is None or pharmacy is None or pharmacy.status != "active":
                raise ValueError("المناوبة أو الصيدلية غير موجودة.")
            old_name = old_shift.pharmacy.name
            await repositories.update_shift(
                session,
                shift_id,
                admin_id=callback.from_user.id,
                pharmacy_id=pharmacy_id,
            )
    except ValueError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return

    async with db.session_factory() as session:
        updated = await repositories.get_shift(session, shift_id)
    if updated is None:
        await answer_callback(callback, "تعذر فتح المناوبة بعد التبديل.", alert=True)
        return
    local_start = as_local(updated.start_at, settings.timezone)
    local_end = as_local(updated.end_at, settings.timezone)
    await safe_edit(
        callback,
        (
            "✅ <b>تم تبديل الصيدلية المناوبة</b>\n\n"
            f"من: {html(old_name)}\n"
            f"إلى: <b>{html(updated.pharmacy.name)}</b>\n"
            f"📅 {format_date_ar(local_start)}\n"
            f"🕐 {format_time_ar(local_start)} – {format_time_ar(local_end)}\n"
            f"📍 {html(updated.pharmacy.address or 'العنوان غير مضاف بعد')}\n\n"
            "لم يتغير التاريخ أو الوقت."
        ),
        keyboards.keyboard(
            [
                [keyboards.button("↩️ التراجع عن التبديل", cb.ADMIN_UNDO, "danger")],
                [keyboards.button("⬅️ رجوع", cb.ADMIN_SHIFTS)],
            ]
        ),
    )
    await answer_callback(callback, "تم تبديل الصيدلية.")
