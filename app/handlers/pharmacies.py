from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.db import Database
from app.handlers.common import require_admin, require_writer
from app.states import PharmacyCreateState, PharmacyEditState, PharmacySearchState
from app.telegram_utils import answer_callback, safe_edit, try_delete
from app.utils import html


router = Router(name="pharmacies")


@router.callback_query(F.data == cb.ADMIN_PHARMACY_ADD)
async def pharmacy_add_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(PharmacyCreateState.waiting_name)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إضافة صيدلية",
            "أرسل الاسم الرسمي للصيدلية. سيطلب البوت بعدها العنوان والأسماء البديلة.",
        ),
        keyboards.simple_back(cb.ADMIN_PHARMACIES),
    )
    await answer_callback(callback)


@router.message(PharmacyCreateState.waiting_name, F.text)
async def pharmacy_add_name(message: Message, bot: Bot, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("اسم الصيدلية قصير جداً.")
        return
    await state.update_data(name=name)
    await state.set_state(PharmacyCreateState.waiting_address)
    data = await state.get_data()
    await _edit_state_message(
        bot,
        message,
        data,
        f"💊 الاسم: <b>{html(name)}</b>\n\n📍 أرسل العنوان الكامل للصيدلية.",
    )
    await try_delete(message)


@router.message(PharmacyCreateState.waiting_address, F.text)
async def pharmacy_add_address(message: Message, bot: Bot, state: FSMContext) -> None:
    address = (message.text or "").strip()
    if len(address) < 3:
        await message.answer("العنوان قصير جداً.")
        return
    await state.update_data(address=address)
    await state.set_state(PharmacyCreateState.waiting_aliases)
    data = await state.get_data()
    await _edit_state_message(
        bot,
        message,
        data,
        "🔤 أرسل الأسماء البديلة مفصولة بفواصل.\nمثال: <code>الشفاء، الشفا</code>\n\nأرسل كلمة <code>لا</code> إذا لا توجد أسماء بديلة.",
    )
    await try_delete(message)


@router.message(PharmacyCreateState.waiting_aliases, F.text)
async def pharmacy_add_aliases(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    raw = (message.text or "").strip()
    aliases = [] if raw in {"لا", "لا يوجد", "بدون"} else [
        item.strip() for item in raw.replace("،", ",").split(",") if item.strip()
    ]
    data = await state.get_data()
    try:
        async with db.session_factory() as session:
            pharmacy = await repositories.create_pharmacy(
                session,
                name=data["name"],
                address=data["address"],
                aliases=aliases,
                admin_id=message.from_user.id,
            )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await _edit_state_message(
        bot,
        message,
        data,
        "✅ <b>تمت إضافة الصيدلية</b>\n\n" + texts.pharmacy_admin_text(pharmacy),
        reply_markup=keyboards.pharmacy_detail(pharmacy.id, pharmacy.status),
    )
    await try_delete(message)
    await state.clear()


@router.callback_query(F.data == cb.ADMIN_PHARMACY_LIST)
async def pharmacy_list_handler(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        pharmacies = await repositories.list_pharmacies(session, limit=500)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "جميع الصيدليات",
            "اضغط على اسم الصيدلية لعرض جميع بياناتها وتعديلها.",
            stats=[f"🏥 الصيدليات المعروضة: {len(pharmacies)}"],
        ),
        keyboards.pharmacy_list(pharmacies),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_PHARMACY_INCOMPLETE)
async def pharmacy_incomplete_handler(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        pharmacies = await repositories.list_pharmacies(session, limit=1000)
    incomplete = [p for p in pharmacies if not p.address.strip() or not p.name.strip()]
    text = texts.admin_section_text(
        "بيانات الصيدليات الناقصة",
        "اضغط على أي صيدلية لإضافة عنوانها أو تعديل الاسم والأسماء البديلة والحالة والملاحظات.",
        stats=[f"⚠️ النتائج: {len(incomplete)}"],
    )
    if not incomplete:
        text += "\n\n✅ جميع الصيدليات تحتوي على اسم وعنوان."
    await safe_edit(callback, text, keyboards.pharmacy_list(incomplete))
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_PHARMACY_SEARCH)
async def pharmacy_search_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_admin(callback, db) is None:
        return
    await state.set_state(PharmacySearchState.waiting_query)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        "🔍 <b>البحث عن صيدلية</b>\n\nأرسل الاسم أو جزءاً من الاسم.",
        keyboards.simple_back(cb.ADMIN_PHARMACIES),
    )
    await answer_callback(callback)


@router.message(PharmacySearchState.waiting_query, F.text)
async def pharmacy_search_result(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    async with db.session_factory() as session:
        pharmacies = await repositories.search_pharmacies(session, message.text or "", limit=30)
    text = texts.admin_section_text(
        "نتيجة بحث الصيدليات",
        "اختر الصيدلية لفتح بياناتها وتعديلها.",
        stats=[f"🔍 النتائج: {len(pharmacies)}"],
    )
    await _edit_state_message(
        bot,
        message,
        data,
        text,
        reply_markup=keyboards.pharmacy_list(pharmacies),
    )
    await try_delete(message)
    await state.clear()


@router.callback_query(F.data.startswith("a:p:view:"))
async def pharmacy_view(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
    if not pharmacy:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    await safe_edit(
        callback,
        texts.pharmacy_admin_text(pharmacy),
        keyboards.pharmacy_detail(pharmacy.id, pharmacy.status),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:p:edit:"))
async def pharmacy_edit_prompt(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    if await require_writer(callback, db) is None:
        return
    _, _, _, pharmacy_id, field = callback.data.split(":")
    prompts = {
        "name": "أرسل الاسم الرسمي الجديد للصيدلية.",
        "address": "أرسل العنوان الجديد للصيدلية.",
        "aliases": "أرسل الأسماء البديلة مفصولة بفواصل، أو أرسل كلمة لا لحذفها.",
        "notes": "أرسل ملاحظات الصيدلية، أو أرسل كلمة لا لحذف الملاحظات.",
    }
    if field not in prompts:
        await answer_callback(callback, "نوع التعديل غير معروف.", alert=True)
        return
    await state.set_state(PharmacyEditState.waiting_value)
    if callback.message:
        await state.update_data(
            menu_message_id=callback.message.message_id,
            chat_id=callback.message.chat.id,
            pharmacy_id=int(pharmacy_id),
            field=field,
        )
    await safe_edit(
        callback,
        f"✏️ <b>تعديل الصيدلية</b>\n\n{prompts[field]}",
        keyboards.simple_back(f"a:p:view:{pharmacy_id}"),
    )
    await answer_callback(callback)


@router.message(PharmacyEditState.waiting_value, F.text)
async def pharmacy_edit_value(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    value = (message.text or "").strip()
    field = data["field"]
    kwargs = {}
    if field == "aliases":
        kwargs["aliases"] = [] if value in {"لا", "لا يوجد", "بدون"} else [
            item.strip() for item in value.replace("،", ",").split(",") if item.strip()
        ]
    elif field == "notes":
        kwargs["notes"] = "" if value in {"لا", "لا يوجد", "بدون"} else value
    else:
        if field == "name" and len(value) < 2:
            await message.answer("اسم الصيدلية قصير جداً.")
            return
        kwargs[field] = value
    try:
        async with db.session_factory() as session:
            pharmacy = await repositories.update_pharmacy(
                session,
                int(data["pharmacy_id"]),
                admin_id=message.from_user.id,
                **kwargs,
            )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await _edit_state_message(
        bot,
        message,
        data,
        "✅ <b>تم تحديث الصيدلية</b>\n\n" + texts.pharmacy_admin_text(pharmacy),
        reply_markup=keyboards.pharmacy_detail(pharmacy.id, pharmacy.status),
    )
    await try_delete(message)
    await state.clear()


@router.callback_query(F.data.startswith("a:p:status_menu:"))
async def pharmacy_status_menu(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
    if pharmacy is None:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    await safe_edit(
        callback,
        "📌 <b>تعديل حالة الصيدلية</b>\n\nاختر الحالة الجديدة:",
        keyboards.pharmacy_status(pharmacy.id, pharmacy.status),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:p:status:"))
async def pharmacy_set_status(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 5:
        await answer_callback(callback, "بيانات الحالة غير صالحة.", alert=True)
        return
    pharmacy_id = int(parts[3])
    status = parts[4]
    try:
        async with db.session_factory() as session:
            pharmacy = await repositories.update_pharmacy(
                session,
                pharmacy_id,
                admin_id=callback.from_user.id,
                status=status,
            )
    except ValueError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return
    await safe_edit(
        callback,
        texts.pharmacy_admin_text(pharmacy),
        keyboards.pharmacy_detail(pharmacy.id, pharmacy.status),
    )
    await answer_callback(callback, "تم تحديث حالة الصيدلية.")


@router.callback_query(F.data.startswith("a:p:toggle:"))
async def pharmacy_toggle(callback: CallbackQuery, db: Database) -> None:
    """Keep the old callback working for messages sent before this update."""
    if await require_writer(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        pharmacy = await repositories.get_pharmacy(session, pharmacy_id)
        if not pharmacy:
            await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
            return
        new_status = "temporarily_closed" if pharmacy.status == "active" else "active"
        pharmacy = await repositories.update_pharmacy(
            session,
            pharmacy_id,
            admin_id=callback.from_user.id,
            status=new_status,
        )
    await safe_edit(
        callback,
        texts.pharmacy_admin_text(pharmacy),
        keyboards.pharmacy_detail(pharmacy.id, pharmacy.status),
    )
    await answer_callback(callback, "تم تحديث الحالة.")


@router.callback_query(F.data.startswith("a:p:delete_ask:"))
async def pharmacy_delete_prompt(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "حذف صيدلية",
            "سيتم إخفاء الصيدلية من البحث والإدارة العادية. يمكن التراجع عن الحذف من لوحة الإدارة.",
            warning="تأكد أولاً أنه لا توجد مناوبات تحتاج إليها.",
        ),
        keyboards.confirm_pharmacy_delete(pharmacy_id),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:p:delete:"))
async def pharmacy_delete_confirm(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    pharmacy_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        success = await repositories.soft_delete_pharmacy(
            session,
            pharmacy_id,
            callback.from_user.id,
        )
    if not success:
        await answer_callback(callback, "الصيدلية غير موجودة.", alert=True)
        return
    await safe_edit(
        callback,
        "✅ تم حذف الصيدلية. يمكنك استعادتها من زر التراجع.",
        keyboards.simple_back(cb.ADMIN_PHARMACIES),
    )
    await answer_callback(callback, "تم الحذف.")


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
                reply_markup=reply_markup or keyboards.simple_back(cb.ADMIN_PHARMACIES),
            )
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=reply_markup or keyboards.simple_back(cb.ADMIN_PHARMACIES))
