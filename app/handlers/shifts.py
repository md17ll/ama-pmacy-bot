from __future__ import annotations

from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.common import require_admin, require_writer
from app.services.validation import detect_shift_conflicts
from app.states import DeletePeriodState, ShiftCreateState, ShiftSearchState
from app.telegram_utils import answer_callback, safe_edit, try_delete
from app.utils import (
    as_local,
    combine_shift,
    format_date_ar,
    format_time_ar,
    local_day_bounds,
    parse_date_value,
    parse_time_value,
    utcnow,
    html,
)


router = Router(name="shifts")


async def _show_shift_list(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    *,
    title: str,
    start_at,
    end_at,
) -> None:
    async with db.session_factory() as session:
        shifts = await repositories.list_shifts_between(session, start_at, end_at, limit=200)
    await safe_edit(
        callback,
        texts.admin_section_text(
            title,
            "اضغط على اسم الصيدلية لفتح المناوبة وتعديلها أو حذفها.",
            stats=[f"📅 عدد المناوبات: {len(shifts)}"],
        ),
        keyboards.shift_list(shifts),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_SHIFT_NOW)
async def admin_shift_now(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    now = utcnow()
    async with db.session_factory() as session:
        shifts = await repositories.current_shifts(session, now)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "المناوبات الحالية",
            "هذه الصيدليات تقع الساعة الحالية ضمن وقت مناوبتها.",
            stats=[f"🟢 المناوبات الجارية: {len(shifts)}"],
        ),
        keyboards.shift_list(shifts),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_SHIFT_TODAY)
async def admin_shift_today(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    today = utcnow().astimezone(settings.timezone).date()
    start, end = local_day_bounds(today, settings.timezone)
    await _show_shift_list(callback, db, settings, title="مناوبات اليوم", start_at=start, end_at=end)


@router.callback_query(F.data == cb.ADMIN_SHIFT_TOMORROW)
async def admin_shift_tomorrow(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    tomorrow = utcnow().astimezone(settings.timezone).date() + timedelta(days=1)
    start, end = local_day_bounds(tomorrow, settings.timezone)
    await _show_shift_list(callback, db, settings, title="مناوبات غداً", start_at=start, end_at=end)


@router.callback_query(F.data == cb.ADMIN_SHIFT_UPCOMING)
async def admin_shift_upcoming(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    now = utcnow()
    end = now + timedelta(days=30)
    await _show_shift_list(
        callback,
        db,
        settings,
        title="المناوبات القادمة",
        start_at=now,
        end_at=end,
    )


def _shift_detail_text(shift, settings: Settings) -> str:
    start = as_local(shift.start_at, settings.timezone)
    end = as_local(shift.end_at, settings.timezone)
    return (
        "📅 <b>تفاصيل المناوبة</b>\n\n"
        f"💊 {html(shift.pharmacy.name)}\n"
        f"📅 {format_date_ar(start)}\n"
        f"🕐 {format_time_ar(start)} – {format_time_ar(end)}\n"
        f"📍 {html(shift.pharmacy.address)}\n"
        f"🆔 رقم المناوبة: {shift.id}"
    )


async def _render_shift_detail(
    callback: CallbackQuery,
    shift,
    settings: Settings,
    *,
    toast: str | None = None,
) -> None:
    await safe_edit(callback, _shift_detail_text(shift, settings), keyboards.shift_detail(shift.id))
    await answer_callback(callback, toast)


@router.callback_query(F.data.startswith("a:s:view:"))
async def shift_view(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    shift_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        shift = await repositories.get_shift(session, shift_id)
    if not shift:
        await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
        return
    await _render_shift_detail(callback, shift, settings)


@router.callback_query(F.data.in_({cb.ADMIN_SHIFT_ADD, cb.ADMIN_IMPORT_MANUAL}))
async def shift_add_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(ShiftCreateState.waiting_pharmacy)
    if callback.message:
        await state.update_data(
            menu_message_id=callback.message.message_id,
            chat_id=callback.message.chat.id,
            mode="create",
        )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إضافة مناوبة يدوياً",
            "أرسل اسم الصيدلية أو جزءاً منه. بعدها سيطلب البوت التاريخ ووقت البداية ووقت النهاية.",
        ),
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback)


@router.message(ShiftCreateState.waiting_pharmacy, F.text)
async def shift_pick_pharmacy(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    async with db.session_factory() as session:
        matches = await repositories.search_pharmacies(session, message.text or "", limit=8)
    if not matches:
        await message.answer("لم أجد صيدلية مطابقة. جرّب كتابة جزء آخر من الاسم.")
        return
    data = await state.get_data()
    menu_message_id = data.get("menu_message_id")
    rows = [[keyboards.button(f"💊 {p.name}", f"a:s:pickp:{p.id}")] for p in matches]
    rows.append([keyboards.button("⬅️ إلغاء", cb.ADMIN_SHIFTS)])
    text = "🏥 <b>اختر الصيدلية</b>\n\nتم العثور على النتائج التالية:"
    if menu_message_id:
        try:
            await bot.edit_message_text(
                chat_id=data.get("chat_id") or message.chat.id,
                message_id=menu_message_id,
                text=text,
                reply_markup=keyboards.keyboard(rows),
            )
        except Exception:
            await message.answer(text, reply_markup=keyboards.keyboard(rows))
    else:
        await message.answer(text, reply_markup=keyboards.keyboard(rows))
    await try_delete(message)


@router.callback_query(F.data.startswith("a:s:pickp:"))
async def shift_pharmacy_selected(callback: CallbackQuery, db: Database, settings: Settings, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
    if not pharmacy:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    data = await state.get_data()
    mode = data.get("mode", "create")
    if mode == "edit_pharmacy":
        shift_id = int(data["shift_id"])
        async with db.session_factory() as session:
            try:
                await repositories.update_shift(
                    session,
                    shift_id,
                    admin_id=callback.from_user.id,
                    pharmacy_id=pharmacy_id,
                )
            except ValueError as exc:
                await answer_callback(callback, str(exc), alert=True)
                return
        await state.clear()
        async with db.session_factory() as session:
            shift = await repositories.get_shift(session, shift_id)
        if not shift:
            await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
            return
        await _render_shift_detail(callback, shift, settings, toast="تم تغيير الصيدلية.")
        return

    await state.update_data(pharmacy_id=pharmacy_id, pharmacy_name=pharmacy.name)
    await state.set_state(ShiftCreateState.waiting_date)
    await safe_edit(
        callback,
        f"✅ تم اختيار <b>{html(pharmacy.name)}</b>\n\n📅 أرسل تاريخ المناوبة.\nمثال: <code>2026-08-05</code> أو <code>5 أغسطس 2026</code>",
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback)


@router.message(ShiftCreateState.waiting_date, F.text)
async def shift_date_received(
    message: Message,
    bot: Bot,
    state: FSMContext,
) -> None:
    try:
        duty_date = parse_date_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل التاريخ مرة أخرى.")
        return
    await state.update_data(duty_date=duty_date.isoformat())
    await state.set_state(ShiftCreateState.waiting_start)
    data = await state.get_data()
    await _edit_state_message(
        bot,
        message,
        data,
        f"📅 التاريخ: <b>{format_date_ar(duty_date)}</b>\n\n🕐 أرسل وقت بداية المناوبة بنظام 12 ساعة.\nمثال: <code>8:00 PM</code> أو <code>8:00 مساءً</code>",
    )
    await try_delete(message)


@router.message(ShiftCreateState.waiting_start, F.text)
async def shift_start_received(message: Message, bot: Bot, state: FSMContext) -> None:
    try:
        start_time = parse_time_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل وقت البداية مرة أخرى.")
        return
    await state.update_data(start_time=start_time.isoformat())
    await state.set_state(ShiftCreateState.waiting_end)
    data = await state.get_data()
    await _edit_state_message(
        bot,
        message,
        data,
        f"🕐 وقت البداية: <b>{format_time_ar(start_time)}</b>\n\nأرسل وقت نهاية المناوبة. إذا كان صباح اليوم التالي سيُحسب تلقائياً.\nمثال: <code>8:00 AM</code>",
    )
    await try_delete(message)


@router.message(ShiftCreateState.waiting_end, F.text)
async def shift_end_received(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    try:
        end_time = parse_time_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}\nأرسل وقت النهاية مرة أخرى.")
        return
    data = await state.get_data()
    duty_date = date.fromisoformat(data["duty_date"])
    start_time = parse_time_value(data["start_time"])
    start_at, end_at = combine_shift(duty_date, start_time, end_time, settings.timezone)
    try:
        async with db.session_factory() as session:
            if data.get("mode") == "edit_datetime":
                shift = await repositories.update_shift(
                    session,
                    int(data["shift_id"]),
                    admin_id=message.from_user.id,
                    start_at=start_at,
                    end_at=end_at,
                )
                pharmacy_name = shift.pharmacy.name
                heading = "✅ <b>تم تعديل المناوبة</b>\n\n"
            else:
                shift = await repositories.create_shift(
                    session,
                    pharmacy_id=int(data["pharmacy_id"]),
                    start_at=start_at,
                    end_at=end_at,
                    admin_id=message.from_user.id,
                )
                pharmacy_name = data["pharmacy_name"]
                heading = "✅ <b>تمت إضافة المناوبة</b>\n\n"
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    result = (
        heading
        + f"💊 {html(pharmacy_name)}\n"
        + f"📅 {format_date_ar(start_at, settings.timezone)}\n"
        + f"🕐 {format_time_ar(start_at, settings.timezone)} – {format_time_ar(end_at, settings.timezone)}"
    )
    await _edit_state_message(
        bot,
        message,
        data,
        result,
        reply_markup=keyboards.shift_detail(shift.id),
    )
    await try_delete(message)
    await state.clear()


@router.callback_query(F.data.startswith("a:s:delete_ask:"))
async def shift_delete_prompt(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    shift_id = int(callback.data.rsplit(":", 1)[1])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "حذف المناوبة",
            "سيتم إخفاء المناوبة عن المستخدمين مع الاحتفاظ بسجل يسمح بالتراجع.",
            warning="أكد الحذف فقط بعد مراجعة المناوبة.",
        ),
        keyboards.confirm_shift_delete(shift_id),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:s:delete:"))
async def shift_delete_confirm(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    shift_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        success = await repositories.delete_shift(session, shift_id, callback.from_user.id)
    if not success:
        await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
        return
    await safe_edit(
        callback,
        "✅ تم حذف المناوبة. يمكنك استعادتها من زر التراجع.",
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback, "تم الحذف.")


@router.callback_query(F.data.startswith("a:s:copy:"))
async def shift_copy(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    shift_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        shift = await repositories.get_shift(session, shift_id)
        if not shift:
            await answer_callback(callback, "المناوبة غير موجودة.", alert=True)
            return
        try:
            copied = await repositories.create_shift(
                session,
                pharmacy_id=shift.pharmacy_id,
                start_at=shift.start_at + timedelta(days=1),
                end_at=shift.end_at + timedelta(days=1),
                admin_id=callback.from_user.id,
            )
        except ValueError as exc:
            await answer_callback(callback, str(exc), alert=True)
            return
    async with db.session_factory() as session:
        copied = await repositories.get_shift(session, copied.id)
    if not copied:
        await answer_callback(callback, "تعذر فتح المناوبة المنسوخة.", alert=True)
        return
    await _render_shift_detail(
        callback,
        copied,
        settings,
        toast="تم نسخ المناوبة إلى اليوم التالي.",
    )


@router.callback_query(F.data.startswith("a:s:edit:"))
async def shift_edit_prompt(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    if await require_writer(callback, db) is None:
        return
    parts = callback.data.split(":")
    shift_id = int(parts[3])
    field = parts[4]
    if callback.message:
        await state.update_data(
            menu_message_id=callback.message.message_id,
            chat_id=callback.message.chat.id,
            shift_id=shift_id,
        )
    if field == "pharmacy":
        await state.update_data(mode="edit_pharmacy")
        await state.set_state(ShiftCreateState.waiting_pharmacy)
        prompt = "🏥 أرسل اسم الصيدلية الجديدة أو جزءاً منه."
    else:
        await state.update_data(mode="edit_datetime")
        await state.set_state(ShiftCreateState.waiting_date)
        prompt = "📅 أرسل التاريخ الجديد، ثم سيطلب البوت وقت البداية والنهاية."
    await safe_edit(callback, prompt, keyboards.simple_back(f"a:s:view:{shift_id}"))
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_SHIFT_SEARCH)
async def shift_search_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_admin(callback, db) is None:
        return
    await state.set_state(ShiftSearchState.waiting_query)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        "🔍 <b>البحث عن مناوبة</b>\n\nأرسل اسم الصيدلية. سيعرض البوت مناوباتها القادمة خلال 90 يوماً.",
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback)


@router.message(ShiftSearchState.waiting_query, F.text)
async def shift_search_result(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    async with db.session_factory() as session:
        pharmacies = await repositories.search_pharmacies(session, message.text or "", limit=1)
        if pharmacies:
            now = utcnow()
            shifts = await repositories.list_shifts_between(session, now, now + timedelta(days=90), limit=500)
            shifts = [shift for shift in shifts if shift.pharmacy_id == pharmacies[0].id]
        else:
            shifts = []
    text = texts.admin_section_text(
        "نتيجة البحث",
        "اختر المناوبة لفتح تفاصيلها.",
        stats=[f"📅 النتائج: {len(shifts)}"],
    )
    await _edit_state_message(
        bot,
        message,
        data,
        text,
        reply_markup=keyboards.shift_list(shifts),
    )
    await try_delete(message)
    await state.clear()


@router.callback_query(F.data == cb.ADMIN_SHIFT_DELETE_PERIOD)
async def delete_period_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(DeletePeriodState.waiting_start)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "حذف فترة من المناوبات",
            "أرسل تاريخ بداية الفترة. سيطلب البوت بعدها تاريخ النهاية ويعرض تأكيداً قبل الحذف.",
            warning="الحذف قابل للتراجع، لكنه يؤثر على جميع المناوبات داخل الفترة.",
        ),
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback)


@router.message(DeletePeriodState.waiting_start, F.text)
async def delete_period_start(message: Message, bot: Bot, state: FSMContext) -> None:
    try:
        start_date = parse_date_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(period_start=start_date.isoformat())
    await state.set_state(DeletePeriodState.waiting_end)
    data = await state.get_data()
    await _edit_state_message(
        bot,
        message,
        data,
        f"📅 بداية الفترة: {format_date_ar(start_date)}\n\nأرسل تاريخ نهاية الفترة.",
    )
    await try_delete(message)


@router.message(DeletePeriodState.waiting_end, F.text)
async def delete_period_end(
    message: Message,
    bot: Bot,
    state: FSMContext,
) -> None:
    try:
        end_date = parse_date_value(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    data = await state.get_data()
    start_date = date.fromisoformat(data["period_start"])
    if end_date < start_date:
        await message.answer("تاريخ النهاية يجب ألا يكون قبل البداية.")
        return
    await state.update_data(period_end=end_date.isoformat())
    text = (
        "🗑 <b>تأكيد حذف فترة</b>\n\n"
        f"من: {format_date_ar(start_date)}\n"
        f"إلى: {format_date_ar(end_date)}\n\n"
        "سيتم إخفاء جميع المناوبات المتداخلة مع هذه الفترة."
    )
    await _edit_state_message(
        bot,
        message,
        data,
        text,
        reply_markup=keyboards.keyboard(
            [
                [keyboards.button("✅ تأكيد الحذف", "a:s:delperiod:confirm", "danger")],
                [keyboards.button("❌ إلغاء", cb.ADMIN_SHIFTS)],
            ]
        ),
    )
    await try_delete(message)


@router.callback_query(F.data == "a:s:delperiod:confirm")
async def delete_period_confirm(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(callback, db) is None:
        return
    data = await state.get_data()
    if "period_start" not in data or "period_end" not in data:
        await answer_callback(callback, "انتهت جلسة الحذف. أعد المحاولة.", alert=True)
        return
    start_date = date.fromisoformat(data["period_start"])
    end_date = date.fromisoformat(data["period_end"])
    start_at, _ = local_day_bounds(start_date, settings.timezone)
    _, end_at = local_day_bounds(end_date, settings.timezone)
    async with db.session_factory() as session:
        count = await repositories.delete_shifts_between(
            session,
            start_at,
            end_at,
            admin_id=callback.from_user.id,
        )
    await state.clear()
    await safe_edit(
        callback,
        f"✅ تم حذف {count} مناوبة من الفترة المحددة. يمكنك استخدام التراجع لاستعادتها.",
        keyboards.simple_back(cb.ADMIN_SHIFTS),
    )
    await answer_callback(callback, "تم الحذف.")


@router.callback_query(F.data == cb.ADMIN_SHIFT_CHECK)
async def shift_check(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    now = utcnow()
    async with db.session_factory() as session:
        shifts = await repositories.list_shifts_between(session, now - timedelta(days=1), now + timedelta(days=60), limit=5000)
    conflicts = detect_shift_conflicts(shifts)
    invalid = [shift for shift in shifts if shift.end_at <= shift.start_at]
    text = texts.admin_section_text(
        "فحص المناوبات",
        "تم فحص المناوبات القادمة بحثاً عن التكرار والتداخل والأوقات غير الصالحة.",
        stats=[
            f"📅 المناوبات المفحوصة: {len(shifts)}",
            f"⚠️ التداخلات: {len(conflicts)}",
            f"❌ أوقات غير صالحة: {len(invalid)}",
        ],
    )
    if not conflicts and not invalid:
        text += "\n\n✅ لم يتم العثور على مشاكل."
    await safe_edit(callback, text, keyboards.simple_back(cb.ADMIN_SHIFTS))
    await answer_callback(callback)


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
