from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.common import require_admin, require_writer
from app.telegram_utils import answer_callback, safe_edit
from app.utils import format_date_ar, format_duration, format_time_ar, utcnow


router = Router(name="admin")


async def _render_admin_home(target: CallbackQuery | Message, db: Database, settings: Settings) -> None:
    if await require_admin(target, db) is None:
        return
    async with db.session_factory() as session:
        stats = await repositories.statistics(session)
    text = texts.admin_home_text(stats, utcnow(), settings.timezone)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, keyboards.admin_home())
        await answer_callback(target)
    else:
        await target.answer(text, reply_markup=keyboards.admin_home())


@router.message(Command("admin"))
async def admin_command(message: Message, db: Database, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await _render_admin_home(message, db, settings)


@router.callback_query(F.data == cb.ADMIN_HOME)
async def admin_home_callback(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    state: FSMContext,
) -> None:
    await state.clear()
    await _render_admin_home(callback, db, settings)


@router.callback_query(F.data == cb.ADMIN_IMPORT)
async def admin_import_menu(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إدخال جدول جديد",
            "اختر طريقة إدخال المناوبات. Excel هو الأدق، وGemini مناسب عندما يصلك الجدول على شكل صورة. كل نتيجة تُحفظ كمسودة ولا تُنشر تلقائياً.",
            stats=[
                "📷 Gemini: قراءة الصورة واستخراج الاسم والتاريخ والوقت.",
                "📊 Excel: فحص الأعمدة والصفوف قبل الحفظ.",
                "✍️ يدوي: إضافة مناوبة واحدة عند الحاجة.",
            ],
        ),
        keyboards.admin_import(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_SHIFTS)
async def admin_shifts_menu(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        count = await repositories.shift_count(session)
        problems = await repositories.count_open_import_errors(session)
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إدارة المناوبات",
            "من هذا القسم يمكنك عرض المناوبات وتعديلها أو إضافة مناوبة جديدة. أي حذف أو تعديل جماعي يحتاج إلى تأكيد.",
            stats=[
                f"📅 المناوبات المسجلة: {count}",
                f"⚠️ المشاكل المكتشفة: {problems}",
            ],
        ),
        keyboards.admin_shifts(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_PHARMACIES)
async def admin_pharmacies_menu(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        count = await repositories.pharmacy_count(session)
        pharmacies = await repositories.list_pharmacies(session, limit=1000)
        missing = sum(1 for pharmacy in pharmacies if not pharmacy.address.strip())
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إدارة الصيدليات",
            "تُحفظ بيانات كل صيدلية مرة واحدة، ثم تُربط المناوبات بها تلقائياً.",
            stats=[
                f"🏥 عدد الصيدليات: {count}",
                f"⚠️ بيانات ناقصة: {missing}",
            ],
        ),
        keyboards.admin_pharmacies(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_ERRORS)
async def admin_errors(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    now = utcnow()
    async with db.session_factory() as session:
        drafts = await repositories.list_draft_batches(session, limit=100)
        open_errors = sum(1 for batch in drafts for row in batch.rows if row.errors)
        latest = await repositories.latest_shift_end(session)
        pharmacies = await repositories.list_pharmacies(session, limit=1000)
    alerts: list[str] = [f"⚠️ أخطاء المسودات: {open_errors}"]
    if latest is None:
        alerts.append("⚠️ لا توجد مناوبات منشورة.")
    elif latest <= now:
        alerts.append("🚨 جدول المناوبات المنشور منتهٍ.")
    elif latest - now <= timedelta(days=2):
        alerts.append(f"⏳ ينتهي الجدول بعد {format_duration(latest - now)}.")
    else:
        alerts.append(f"✅ الجدول ممتد حتى {format_date_ar(latest, settings.timezone)}.")
    missing_addresses = [pharmacy.name for pharmacy in pharmacies if not pharmacy.address.strip()]
    alerts.append(f"📍 صيدليات بدون عنوان: {len(missing_addresses)}")
    if open_errors:
        alerts.append("اضغط المسودات ثم افتح المسودة لمراجعة السطور المعلّمة.")
    await safe_edit(
        callback,
        texts.admin_section_text(
            "الأخطاء والتنبيهات",
            "يعرض هذا القسم المشاكل التي تحتاج إلى تدخل الإدارة قبل أن تؤثر على المستخدمين.",
            stats=alerts,
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📝 فتح المسودات", cb.ADMIN_DRAFTS)],
                [keyboards.button("⚠️ فحص المناوبات", cb.ADMIN_SHIFT_CHECK)],
                [keyboards.button("⬅️ رجوع", cb.ADMIN_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_UNDO)
async def undo_prompt(callback: CallbackQuery, db: Database) -> None:
    if await require_writer(callback, db) is None:
        return
    async with db.session_factory() as session:
        audit = await repositories.get_last_reversible_audit(session, callback.from_user.id)
    if audit is None:
        await answer_callback(callback, "لا توجد عملية قابلة للتراجع.", alert=True)
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "التراجع عن آخر عملية",
            "سيعيد البوت آخر عملية قابلة للتراجع نفذتها أنت. راجع نوع العملية قبل التأكيد.",
            stats=[f"↩️ العملية: {audit.action}", f"🆔 السجل: {audit.id}"],
            warning="لا تضغط التأكيد إلا إذا كنت متأكداً.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("✅ تأكيد التراجع", "a:undo:confirm", "danger")],
                [keyboards.button("❌ إلغاء", cb.ADMIN_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:undo:confirm")
async def undo_confirm(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        async with db.session_factory() as session:
            action = await repositories.undo_last_action(session, callback.from_user.id)
    except ValueError as exc:
        await answer_callback(callback, str(exc), alert=True)
        return
    await answer_callback(callback, "تم التراجع بنجاح.")
    await _render_admin_home(callback, db, settings)


@router.callback_query(F.data == cb.ADMIN_PREVIEW)
async def admin_preview(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    now = utcnow()
    async with db.session_factory() as session:
        last_update = await repositories.latest_published_at(session)
    text = "👁 <b>معاينة واجهة المستخدم</b>\n\n" + texts.user_home_text(
        now, settings.timezone, last_update
    )
    await safe_edit(
        callback,
        text,
        keyboards.keyboard(
            [
                [keyboards.button("🌙 المناوبة الآن", cb.USER_NOW, "primary")],
                [
                    keyboards.button("📅 اليوم", cb.USER_TODAY),
                    keyboards.button("⏭ غداً", cb.USER_TOMORROW),
                ],
                [keyboards.button("🔍 البحث", cb.USER_SEARCH, "primary")],
                [keyboards.button("⬅️ الرجوع للإدارة", cb.ADMIN_HOME)],
            ]
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_EXPORTS)
async def admin_exports_menu(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "النسخ والتصدير",
            "يمكنك تنزيل بيانات الصيدليات والمناوبات كملفات Excel، أو إنشاء نسخة JSON كاملة للرجوع إليها.",
            warning="احتفظ بالنسخ الاحتياطية في مكان آمن.",
        ),
        keyboards.exports(),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_STATS)
async def admin_stats(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        stats = await repositories.statistics(session)
        usage = await repositories.usage_statistics(session)
    latest = stats["latest_shift_end"]
    lines = [
        f"👥 المستخدمون: {stats['users']}",
        f"🏥 الصيدليات: {stats['pharmacies']}",
        f"📅 المناوبات النشطة: {stats['shifts']}",
        f"📝 المسودات: {stats['drafts']}",
        f"⚠️ الأخطاء: {stats['errors']}",
        f"👤 الإداريون: {stats['admins']}",
        f"🚀 مرات الضغط على Start: {usage['starts']}",
        f"🔍 عمليات البحث: {usage['searches']}",
    ]
    if usage["popular_actions"]:
        action_names = {
            "view_now": "المناوبة الآن",
            "view_today": "صيدليات اليوم",
            "view_tomorrow": "صيدليات غداً",
            "user_search": "البحث",
        }
        top_action, top_count = usage["popular_actions"][0]
        lines.append(f"⭐ الأكثر استخداماً: {action_names.get(top_action, top_action)} ({top_count})")
    if latest:
        lines.append(
            f"🕐 آخر مناوبة: {format_date_ar(latest, settings.timezone)}، {format_time_ar(latest, settings.timezone)}"
        )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "الإحصائيات",
            "ملخص سريع لحالة البوت وقاعدة البيانات. هذه البيانات لا تظهر للمستخدمين.",
            stats=lines,
        ),
        keyboards.simple_back(cb.ADMIN_HOME),
    )
    await answer_callback(callback)


@router.callback_query(F.data == cb.ADMIN_ENTRY_NOTIFICATIONS)
async def entry_notification_settings(callback: CallbackQuery, db: Database) -> None:
    admin = await require_admin(callback, db)
    if admin is None:
        return
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إشعارات دخول المستخدمين",
            "عند تشغيلها يصلك إشعار مرة واحدة فقط عند أول دخول لمستخدم جديد، ولا يتكرر الإشعار عند كل ضغطة Start.",
            stats=[
                f"🔔 الحالة: {'مفعلة' if admin.entry_notifications else 'متوقفة'}",
                "👤 البيانات: الاسم، المعرف، رقم مستخدم تلغرام، اللغة، المصدر والتاريخ.",
            ],
        ),
        keyboards.notifications(admin.entry_notifications),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "a:n:toggle")
async def entry_notification_toggle(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    async with db.session_factory() as session:
        enabled = await repositories.toggle_entry_notifications(session, callback.from_user.id)
    await answer_callback(callback, "تم تحديث الإعداد.")
    await safe_edit(
        callback,
        texts.admin_section_text(
            "إشعارات دخول المستخدمين",
            "يصلك إشعار عند أول دخول لمستخدم جديد فقط.",
            stats=[f"🔔 الحالة: {'مفعلة' if enabled else 'متوقفة'}"],
        ),
        keyboards.notifications(enabled),
    )
