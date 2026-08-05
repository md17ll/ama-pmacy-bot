from __future__ import annotations

from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app import keyboards, repositories, texts
from app.db import Database
from app.handlers.common import require_writer
from app.services.excel import (
    MAX_XLSX_BYTES,
    build_missing_pharmacies_template,
    parse_pharmacies_workbook,
)
from app.services.pharmacy_autocreate import create_missing_pharmacy_names
from app.states import AdminImportState
from app.telegram_utils import answer_callback, safe_edit, try_delete


router = Router(name="missing_pharmacies")


async def _download_file(bot: Bot, file_id: str) -> bytes:
    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue()


@router.callback_query(F.data.startswith("a:d:auto_pharmacies:"))
async def auto_create_pharmacies_from_draft(
    callback: CallbackQuery,
    db: Database,
) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        batch_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await answer_callback(callback, "رقم المسودة غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)
        if batch is None or batch.status != "draft":
            await answer_callback(callback, "المسودة غير موجودة.", alert=True)
            return

        names = [
            row.raw_pharmacy_name
            for row in batch.rows
            if not row.matched_pharmacy_id and row.raw_pharmacy_name.strip()
        ]
        if not names:
            await answer_callback(callback, "كل أسماء الصيدليات محفوظة مسبقاً.", alert=True)
            return

        created, existing = await create_missing_pharmacy_names(
            session,
            names,
            admin_id=callback.from_user.id,
            source_note=f"أضيفت تلقائياً من المسودة رقم {batch_id}",
        )
        matched = await repositories.rematch_import_batch(
            session,
            batch_id,
            callback.from_user.id,
        )
        batch = await repositories.get_import_batch(session, batch_id)

    await safe_edit(
        callback,
        "✅ <b>تم حفظ أسماء الصيدليات</b>\n\n"
        f"🏥 أسماء جديدة أضيفت: {created}\n"
        f"🔗 مناوبات تمت مطابقتها: {matched}\n"
        f"ℹ️ أسماء موجودة مسبقاً: {existing}\n\n"
        + texts.batch_summary_text(batch)
        + "\n\n📍 أضف العناوين من: إدارة الصيدليات ← بيانات ناقصة.",
        keyboards.draft_detail(batch_id),
    )
    await answer_callback(callback, "تم حفظ الأسماء وإعادة مطابقة المسودة.")


@router.callback_query(F.data.startswith("a:d:missing:"))
async def missing_pharmacies_prompt(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
) -> None:
    if await require_writer(callback, db) is None:
        return

    try:
        batch_id = int((callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await answer_callback(callback, "رقم المسودة غير صالح.", alert=True)
        return

    async with db.session_factory() as session:
        batch = await repositories.get_import_batch(session, batch_id)

    if batch is None or batch.status != "draft":
        await answer_callback(callback, "المسودة غير موجودة.", alert=True)
        return

    names = sorted(
        {
            row.raw_pharmacy_name.strip()
            for row in batch.rows
            if not row.matched_pharmacy_id and row.raw_pharmacy_name.strip()
        }
    )
    if not names:
        await answer_callback(callback, "لا توجد صيدليات غير مطابقة في هذه المسودة.", alert=True)
        return

    await state.set_state(AdminImportState.waiting_missing_pharmacies_excel)
    await state.update_data(rematch_batch_id=batch_id)

    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(
                build_missing_pharmacies_template(names, batch_id),
                filename=f"missing_pharmacies_batch_{batch_id}.xlsx",
            ),
            caption=(
                "🏥 <b>صيدليات غير مطابقة من المسودة</b>\n\n"
                "أسماء الصيدليات موجودة مسبقاً داخل الملف. عبّئ عمود العنوان، "
                "وأضف أسماء بديلة عند الحاجة، ثم أرسل الملف نفسه هنا.\n\n"
                "بعد الرفع سيضيف البوت الصيدليات ويعيد مطابقة المسودة تلقائياً."
            ),
        )

    await safe_edit(
        callback,
        texts.admin_section_text(
            "إضافة الصيدليات عبر Excel",
            f"تم تجهيز ملف يحتوي على {len(names)} اسماً غير مطابق في المسودة #{batch_id}.",
            warning="عبّئ عنوان كل صيدلية ثم أرسل ملف Excel نفسه في هذه المحادثة.",
        ),
        keyboards.simple_back(f"a:d:view:{batch_id}"),
    )
    await answer_callback(callback, "تم إرسال ملف الصيدليات.")


@router.message(AdminImportState.waiting_missing_pharmacies_excel, F.document)
async def missing_pharmacies_receive(
    message: Message,
    bot: Bot,
    db: Database,
    state: FSMContext,
) -> None:
    if await require_writer(message, db) is None:
        return

    state_data = await state.get_data()
    batch_id = int(state_data.get("rematch_batch_id") or 0)
    if not batch_id:
        await message.answer("تعذر تحديد المسودة. افتح المسودة وأعد المحاولة.")
        await state.clear()
        return

    document = message.document
    filename = document.file_name or "missing_pharmacies.xlsx"
    if not filename.lower().endswith(".xlsx"):
        await message.answer("الملف يجب أن يكون بصيغة xlsx.")
        return
    if document.file_size and document.file_size > MAX_XLSX_BYTES:
        await message.answer("حجم ملف Excel أكبر من الحد المسموح.")
        return

    status_message = await message.answer("⏳ جاري إضافة الصيدليات وإعادة مطابقة المسودة…")
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

            matched = await repositories.rematch_import_batch(
                session,
                batch_id,
                message.from_user.id,
            )
            batch = await repositories.get_import_batch(session, batch_id)
    except Exception as exc:
        await status_message.edit_text(
            "⚠️ تعذر استيراد الصيدليات.\n\n"
            f"السبب: {exc}",
            reply_markup=keyboards.simple_back(f"a:d:view:{batch_id}"),
        )
        return

    details = "\n".join(f"• {item}" for item in skipped[:8])
    summary = texts.batch_summary_text(batch) if batch else f"📝 المسودة #{batch_id}"
    await status_message.edit_text(
        "✅ <b>تمت معالجة الصيدليات</b>\n\n"
        f"➕ صيدليات أضيفت: {added}\n"
        f"🔗 سطور تمت مطابقتها: {matched}\n"
        f"⏭ سطور تم تجاوزها: {len(skipped)}\n\n"
        f"{summary}"
        + (f"\n\n{details}" if details else ""),
        reply_markup=keyboards.draft_detail(batch_id),
    )
    await try_delete(message)
    await state.clear()


@router.message(AdminImportState.waiting_missing_pharmacies_excel)
async def missing_pharmacies_wrong_input(message: Message) -> None:
    await message.answer("أرسل ملف Excel الذي جهزه البوت بصيغة xlsx.")
