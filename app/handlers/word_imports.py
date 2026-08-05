from __future__ import annotations

from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.common import require_writer
from app.services.importer import prepare_import_rows
from app.services.pharmacy_autocreate import create_missing_pharmacy_names
from app.services.word_schedule import MAX_DOCX_BYTES, parse_amuda_word_schedule
from app.states import AdminImportState
from app.telegram_utils import answer_callback, safe_edit, try_delete


router = Router(name="word_imports")


async def _download_file(bot: Bot, file_id: str) -> bytes:
    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


@router.callback_query(F.data == cb.ADMIN_IMPORT_WORD)
async def word_prompt(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if await require_writer(callback, db) is None:
        return
    await state.set_state(AdminImportState.waiting_word)
    if callback.message:
        await state.update_data(
            menu_message_id=callback.message.message_id,
            chat_id=callback.message.chat.id,
        )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "رفع جدول Word الرسمي",
            "أرسل ملف docx بنفس تنسيق جدول صيدليات عامودا: كل مجموعة أعمدة تحتوي التاريخ، الصيدلية النهارية، والصيدلية المسائية.",
            stats=[
                "☀️ النهارية: تُقرأ من الوقت المكتوب في رأس الجدول.",
                "🌙 المسائية: تُقرأ من الوقت المكتوب في رأس الجدول.",
                "🏥 أسماء الصيدليات الجديدة تُحفظ تلقائياً.",
                "↔️ يدعم وجود مجموعتين من التواريخ جنب بعض في الصفحة.",
            ],
            warning="العناوين غير الموجودة تبقى فارغة وتظهر في قسم بيانات ناقصة لتعديلها لاحقاً.",
        ),
        keyboards.simple_back(cb.ADMIN_IMPORT),
    )
    await answer_callback(callback)


@router.message(AdminImportState.waiting_word, F.document)
async def word_receive_document(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return
    document = message.document
    filename = document.file_name or "amuda_schedule.docx"
    if not filename.lower().endswith(".docx"):
        await message.answer("الملف يجب أن يكون بصيغة docx.")
        return
    if document.file_size and document.file_size > MAX_DOCX_BYTES:
        await message.answer("حجم ملف Word أكبر من الحد المسموح.")
        return

    status = await message.answer("⏳ جاري قراءة جدول Word وحفظ أسماء الصيدليات…")
    auto_created = 0
    try:
        data = await _download_file(bot, document.file_id)
        parsed_rows, warnings = parse_amuda_word_schedule(data)
        async with db.session_factory() as session:
            prepared = await prepare_import_rows(session, parsed_rows, settings.timezone)
            missing_names = [
                row["raw_pharmacy_name"]
                for row in prepared
                if not row.get("matched_pharmacy_id")
            ]
            if missing_names:
                auto_created, _ = await create_missing_pharmacy_names(
                    session,
                    missing_names,
                    admin_id=message.from_user.id,
                    source_note=f"أضيفت تلقائياً من جدول Word: {filename}",
                )
                prepared = await prepare_import_rows(session, parsed_rows, settings.timezone)

            batch = await repositories.create_import_batch(
                session,
                source_type="word",
                source_name=filename,
                source_file_id=document.file_id,
                created_by=message.from_user.id,
                rows=prepared,
            )
    except Exception as exc:
        await status.edit_text(
            "⚠️ تعذر قراءة جدول Word.\n\n"
            f"السبب: {exc}\n\n"
            "تأكد أن الجدول يحتوي أعمدة: التاريخ، النهارية، المساء.",
            reply_markup=keyboards.simple_back(cb.ADMIN_IMPORT),
        )
        return

    details = ""
    if auto_created:
        details += (
            f"\n\n🏥 تم حفظ {auto_created} اسم صيدلية جديد تلقائياً."
            "\n📍 أضف عناوينها من: إدارة الصيدليات ← بيانات ناقصة."
        )
    if warnings:
        details += "\n\n⚠️ ملاحظات ملف Word:\n" + "\n".join(
            f"• {item}" for item in warnings[:8]
        )
    await status.edit_text(
        texts.batch_summary_text(batch) + details,
        reply_markup=keyboards.draft_detail(batch.id),
    )
    await try_delete(message)
    await state.clear()


@router.message(AdminImportState.waiting_word)
async def word_wrong_input(message: Message) -> None:
    await message.answer("أرسل ملف جدول Word بصيغة docx.")
