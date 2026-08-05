from __future__ import annotations

from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.common import require_admin, require_writer
from app.services.excel import (
    MAX_XLSX_BYTES,
    build_pharmacies_template,
    build_shifts_template,
    parse_pharmacies_workbook,
    parse_shifts_workbook,
)
from app.services.gemini import MAX_IMAGE_BYTES, GeminiScheduleReader
from app.services.importer import prepare_import_rows
from app.states import AdminImportState
from app.telegram_utils import answer_callback, safe_edit, try_delete


router = Router(name="imports")


async def _download_file(bot: Bot, file_id: str) -> bytes:
    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


@router.callback_query(F.data == cb.ADMIN_IMPORT_GEMINI)
async def gemini_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(AdminImportState.waiting_image)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "قراءة صورة بواسطة Gemini",
            "أرسل الآن صورة واضحة وكاملة لجدول المناوبات. سيستخرج Gemini اسم الصيدلية والتاريخ ووقت البداية والنهاية فقط، ثم يحفظ النتيجة كمسودة للمراجعة.",
            warning="لن تُنشر أي بيانات قبل موافقتك.",
        ),
        keyboards.simple_back(cb.ADMIN_IMPORT),
    )
    await answer_callback(callback)


@router.message(AdminImportState.waiting_image, F.photo)
async def gemini_receive_photo(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    gemini_reader: GeminiScheduleReader,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        await message.answer("حجم الصورة أكبر من الحد المسموح.")
        return
    status = await message.answer("⏳ جاري تنزيل الصورة وتحليلها بواسطة Gemini…")
    try:
        data = await _download_file(bot, photo.file_id)
        parsed_rows, warnings = await gemini_reader.read_image(data, "image/jpeg")
        async with db.session_factory() as session:
            prepared = await prepare_import_rows(session, parsed_rows, settings.timezone)
            batch = await repositories.create_import_batch(
                session,
                source_type="gemini",
                source_name="صورة جدول المناوبات",
                source_file_id=photo.file_id,
                created_by=message.from_user.id,
                rows=prepared,
            )
    except Exception as exc:
        await status.edit_text(
            "⚠️ تعذر تحليل الصورة.\n\n"
            f"السبب: {exc}\n\n"
            "أعد إرسال صورة أوضح، أو استخدم ملف Excel.",
            reply_markup=keyboards.simple_back(cb.ADMIN_IMPORT),
        )
        return
    warning_text = ""
    if warnings:
        warning_text = "\n\n⚠️ تحذيرات Gemini:\n" + "\n".join(f"• {item}" for item in warnings[:5])
    await status.edit_text(
        texts.batch_summary_text(batch) + warning_text,
        reply_markup=keyboards.draft_detail(batch.id),
    )
    await try_delete(message)
    await state.clear()


@router.message(AdminImportState.waiting_image)
async def gemini_wrong_input(message: Message) -> None:
    await message.answer("أرسل صورة للجدول، وليس نصاً أو ملفاً آخر.")


@router.callback_query(F.data == cb.ADMIN_IMPORT_EXCEL)
async def excel_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(AdminImportState.waiting_excel)
    if callback.message:
        await state.update_data(menu_message_id=callback.message.message_id, chat_id=callback.message.chat.id)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "رفع ملف Excel للمناوبات",
            "أرسل ملف xlsx يحتوي على الأعمدة: اسم الصيدلية، التاريخ، وقت البداية، وقت النهاية. العناوين تُجلب من قاعدة البيانات ولا تتكرر في الملف.",
            warning="استخدم النموذج الجاهز لتجنب أخطاء الأعمدة.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📥 تنزيل النموذج", cb.ADMIN_TEMPLATE_SHIFTS)],
                [keyboards.button("⬅️ رجوع", cb.ADMIN_IMPORT)],
            ]
        ),
    )
    await answer_callback(callback)


@router.message(AdminImportState.waiting_excel, F.document)
async def excel_receive_document(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    document = message.document
    filename = document.file_name or "shifts.xlsx"
    if not filename.lower().endswith(".xlsx"):
        await message.answer("الملف يجب أن يكون بصيغة xlsx.")
        return
    if document.file_size and document.file_size > MAX_XLSX_BYTES:
        await message.answer("حجم ملف Excel أكبر من الحد المسموح.")
        return
    status = await message.answer("⏳ جاري فحص ملف Excel…")
    try:
        data = await _download_file(bot, document.file_id)
        parsed_rows = parse_shifts_workbook(data)
        async with db.session_factory() as session:
            prepared = await prepare_import_rows(session, parsed_rows, settings.timezone)
            batch = await repositories.create_import_batch(
                session,
                source_type="excel",
                source_name=filename,
                source_file_id=document.file_id,
                created_by=message.from_user.id,
                rows=prepared,
            )
    except Exception as exc:
        await status.edit_text(
            f"⚠️ تعذر قراءة ملف Excel.\n\nالسبب: {exc}",
            reply_markup=keyboards.simple_back(cb.ADMIN_IMPORT),
        )
        return
    await status.edit_text(texts.batch_summary_text(batch), reply_markup=keyboards.draft_detail(batch.id))
    await try_delete(message)
    await state.clear()


@router.message(AdminImportState.waiting_excel)
async def excel_wrong_input(message: Message) -> None:
    await message.answer("أرسل ملف Excel بصيغة xlsx.")


@router.callback_query(F.data == cb.ADMIN_TEMPLATE_SHIFTS)
async def download_shifts_template(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(build_shifts_template(), filename="amuda_shifts_template.xlsx"),
            caption="📊 نموذج Excel للمناوبات. العناوين تُجلب من قاعدة البيانات.",
        )
    await answer_callback(callback, "تم إرسال النموذج.")


@router.callback_query(F.data == cb.ADMIN_TEMPLATE_PHARMACIES)
async def download_pharmacies_template(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(build_pharmacies_template(), filename="amuda_pharmacies_template.xlsx"),
            caption="🏥 نموذج Excel للصيدليات: الاسم، العنوان، الأسماء البديلة، الحالة والملاحظات.",
        )
    await answer_callback(callback, "تم إرسال النموذج.")


@router.callback_query(F.data == cb.ADMIN_DRAFTS)
async def list_drafts(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        batches = await repositories.list_draft_batches(session)
    text = texts.admin_section_text(
        "المسودات",
        "هنا تظهر الجداول التي تم رفعها ولم تُنشر بعد. افتح المسودة لمراجعة الأخطاء والمعاينة ثم اختر طريقة النشر.",
        stats=[f"📝 عدد المسودات: {len(batches)}"],
    )
    if not batches:
        text += "\n\n✅ لا توجد مسودات حالياً."
    await safe_edit(callback, text, keyboards.drafts(batches))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:d:view:"))
async def draft_view(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if not batch:
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return
    await safe_edit(callback, texts.batch_summary_text(batch), keyboards.draft_detail(batch.id))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:d:preview:"))
async def draft_preview(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if not batch:
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return
    await safe_edit(
        callback,
        texts.batch_rows_preview(batch, settings.timezone),
        keyboards.simple_back(f"a:d:view:{batch_id}"),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:d:errors:"))
async def draft_errors(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
    if not batch:
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return
    await safe_edit(
        callback,
        texts.batch_rows_preview(batch, settings.timezone, only_errors=True),
        keyboards.keyboard(
            [
                [keyboards.button("🔄 إعادة مطابقة الأسماء", f"a:d:rematch:{batch_id}", "primary")],
                [keyboards.button("🏥 إدارة الصيدليات", cb.ADMIN_PHARMACIES)],
                [keyboards.button("⬅️ رجوع", f"a:d:view:{batch_id}")],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:d:rematch:"))
async def draft_rematch(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        count = await repositories.rematch_import_batch(session, batch_id, callback.from_user.id)
        batch = await repositories.get_import_batch(session, batch_id)
    await answer_callback(callback, f"تمت مطابقة {count} سطر.")
    if batch:
        await safe_edit(callback, texts.batch_summary_text(batch), keyboards.draft_detail(batch.id))


@router.callback_query(F.data.startswith("a:d:publish_ask:"))
async def publish_prompt(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    _, _, _, batch_id, mode = callback.data.split(":")
    mode_text = "إضافة المناوبات الصحيحة إلى الموجود" if mode == "add" else "استبدال المناوبات المتداخلة مع فترة المسودة"
    await safe_edit(
        callback,
        texts.admin_section_text(
            "تأكيد نشر المسودة",
            f"طريقة النشر: {mode_text}.",
            warning="سيتم إنشاء نسخة من العملية في سجل التراجع.",
        ),
        keyboards.confirm_publish(int(batch_id), mode),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:d:publish:"))
async def publish_confirm(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    _, _, _, batch_id, mode = callback.data.split(":")
    try:
        async with db.session_factory() as session:
            inserted, removed = await repositories.publish_import_batch(
                session,
                int(batch_id),
                admin_id=callback.from_user.id,
                replace_period=mode == "replace",
            )
    except ValueError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return
    await safe_edit(
        callback,
        "✅ <b>تم نشر جدول المناوبات</b>\n\n"
        f"➕ تمت إضافة: {inserted}\n"
        f"🗑 تم استبدال/إيقاف: {removed}\n\n"
        "أصبح الجدول ظاهراً للمستخدمين.",
        keyboards.simple_back(cb.ADMIN_HOME),
    )
    await answer_callback(callback, "تم النشر بنجاح.")


@router.callback_query(F.data.startswith("a:d:cancel_ask:"))
async def cancel_batch_prompt(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    await safe_edit(
        callback,
        texts.admin_section_text(
            "حذف المسودة",
            "سيتم إلغاء المسودة ولن تظهر ضمن المسودات النشطة. لن تتأثر المناوبات المنشورة.",
            warning="تحتاج هذه العملية إلى تأكيد.",
        ),
        keyboards.confirm_cancel_batch(batch_id),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:d:cancel:"))
async def cancel_batch_confirm(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    batch_id = int(callback.data.rsplit(":", 1)[1])
    async with db.session_factory() as session:
        success = await repositories.cancel_import_batch(session, batch_id, callback.from_user.id)
    if not success:
        await answer_callback(callback, "تعذر حذف المسودة.", alert=True)
        return
    await answer_callback(callback, "تم حذف المسودة.")
    async with db.session_factory() as session:
        batches = await repositories.list_draft_batches(session)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "المسودات",
            "هنا تظهر الجداول غير المنشورة.",
            stats=[f"📝 عدد المسودات: {len(batches)}"],
        ),
        keyboards.drafts(batches),
    )


@router.callback_query(F.data == cb.ADMIN_PHARMACY_IMPORT)
async def pharmacy_excel_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(AdminImportState.waiting_pharmacies_excel)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "استيراد الصيدليات من Excel",
            "أرسل ملف xlsx يحتوي على اسم الصيدلية والعنوان. يمكن إضافة أسماء بديلة وحالة وملاحظات.",
            warning="الأسماء المكررة لن تُضاف.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📥 تنزيل النموذج", cb.ADMIN_TEMPLATE_PHARMACIES)],
                [keyboards.button("⬅️ رجوع", cb.ADMIN_PHARMACIES)],
            ]
        ),
    )
    await answer_callback(callback)


@router.message(AdminImportState.waiting_pharmacies_excel, F.document)
async def pharmacy_excel_receive(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    document = message.document
    if not (document.file_name or "").lower().endswith(".xlsx"):
        await message.answer("أرسل ملفاً بصيغة xlsx.")
        return
    if document.file_size and document.file_size > MAX_XLSX_BYTES:
        await message.answer("حجم ملف Excel أكبر من الحد المسموح.")
        return
    status_message = await message.answer("⏳ جاري استيراد الصيدليات…")
    added = 0
    skipped: list[str] = []
    try:
        data = await _download_file(bot, document.file_id)
        rows = parse_pharmacies_workbook(data)
        async with db.session_factory() as session:
            for row in rows:
                try:
                    await repositories.create_pharmacy(
                        session,
                        name=row["name"],
                        address=row["address"],
                        aliases=row["aliases"],
                        status=row["status"],
                        notes=row["notes"],
                        admin_id=message.from_user.id,
                    )
                    added += 1
                except ValueError as exc:
                    skipped.append(f"السطر {row['row_number']}: {exc}")
    except Exception as exc:
        await status_message.edit_text(f"⚠️ تعذر استيراد الملف.\n\n{exc}")
        return
    details = "\n".join(f"• {item}" for item in skipped[:10])
    await status_message.edit_text(
        "✅ <b>اكتمل استيراد الصيدليات</b>\n\n"
        f"➕ تمت الإضافة: {added}\n"
        f"⏭ تم التجاوز: {len(skipped)}"
        + (f"\n\n{details}" if details else ""),
        reply_markup=keyboards.simple_back(cb.ADMIN_PHARMACIES),
    )
    await try_delete(message)
    await state.clear()
