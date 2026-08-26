from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import callbacks as cb, keyboards, public_keyboards, repositories, texts
from app.config import Settings
from app.db import Database
from app.handlers.common import require_admin, require_writer
from app.telegram_utils import answer_callback, safe_edit
from app.utils import (
    as_local,
    format_date_ar,
    format_datetime_ar,
    format_duration,
    format_time_ar,
    html,
    local_day_bounds,
    utcnow,
)


router = Router(name="admin")
ACTIVE_USERS_PAGE_SIZE = 8

BUTTON_NAMES = {
    cb.USER_HOME: "الرجوع للواجهة الرئيسية",
    cb.USER_NOW: "الصيدليات المناوبة الآن",
    cb.USER_TODAY: "صيدليات اليوم",
    cb.USER_TOMORROW: "صيدليات غداً",
    cb.USER_SEARCH: "البحث عن صيدلية",
    cb.USER_REFRESH: "تحديث الوقت",
    "u:pinfo": "فتح بيانات صيدلية",
}

EVENT_NAMES = {
    "start": "فتح البوت /start",
    "view_now": "عرض المناوبة الآن",
    "view_today": "عرض صيدليات اليوم",
    "view_tomorrow": "عرض صيدليات غداً",
    "user_search": "تنفيذ بحث",
    "message_activity": "إرسال رسالة أو ملف",
}


def _activity_since(days: int, settings: Settings):
    now = utcnow()
    if days == 0:
        return None
    if days == 1:
        return local_day_bounds(now.astimezone(settings.timezone).date(), settings.timezone)[0]
    return now - timedelta(days=days)


def _period_name(days: int) -> str:
    return {1: "اليوم", 7: "آخر 7 أيام", 30: "آخر 30 يومًا", 0: "كل الوقت"}.get(
        days,
        "آخر 7 أيام",
    )


def _valid_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        return 7
    return days if days in {0, 1, 7, 30} else 7


def _user_name(user) -> str:
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return name or (f"@{user.username}" if user.username else str(user.telegram_id))


def _activity_event_name(event) -> str:
    if event.event == "button_click":
        details = event.event_data or {}
        action = str(details.get("action") or "")
        return f"ضغط زر: {BUTTON_NAMES.get(action, details.get('button_text') or action or 'زر')}"
    if event.event == "user_search":
        found = (event.event_data or {}).get("pharmacy_id") is not None
        return "بحث ناجح" if found else "بحث بدون نتيجة"
    return EVENT_NAMES.get(event.event, event.event)


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
            "اختر طريقة إدخال المناوبات. يمكنك رفع جدول Word الرسمي أو إضافة مناوبة واحدة يدوياً. كل نتيجة تُراجع قبل نشرها.",
            stats=[
                "📄 Word: قراءة الجدول الرسمي وحفظه كمسودة.",
                "✍️ يدوي: إضافة مناوبة واحدة عند الحاجة.",
            ],
        ),
        keyboards.admin_import(),
    )
    await answer_callback(callback)


@router.callback_query(
    F.data.in_({cb.ADMIN_IMPORT_GEMINI, cb.ADMIN_IMPORT_EXCEL, cb.ADMIN_TEMPLATE_SHIFTS})
)
async def removed_import_feature(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    await answer_callback(callback, "تم حذف هذه الميزة من البوت.", alert=True)


@router.callback_query(F.data.startswith("a:smart"))
async def removed_smart_schedule_feature(callback: CallbackQuery, db: Database) -> None:
    if await require_admin(callback, db) is None:
        return
    await answer_callback(callback, "تم حذف مولّد الجداول الذكي من البوت.", alert=True)


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
    """Render the real public home view so preview always follows every UI update."""
    if await require_admin(callback, db) is None:
        return
    now = utcnow()
    async with db.session_factory() as session:
        current_shifts = await repositories.current_shifts(session, now)
        premium_emoji_id = await repositories.get_setting(
            session,
            "developer_button_emoji_id",
            None,
        )
    await safe_edit(
        callback,
        texts.user_home_text(now, settings.timezone, current_shifts),
        public_keyboards.user_preview(
            premium_emoji_id=str(premium_emoji_id) if premium_emoji_id else None,
        ),
    )
    await answer_callback(callback, "هذه هي واجهة المستخدم الحالية.")


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
        today = await repositories.activity_overview(
            session,
            since=_activity_since(1, settings),
        )
        week = await repositories.activity_overview(
            session,
            since=_activity_since(7, settings),
        )
    latest = stats["latest_shift_end"]
    lines = [
        f"👥 المستخدمون: {stats['users']}",
        f"🟢 النشطون اليوم: {today['active_users']}",
        f"📆 النشطون آخر 7 أيام: {week['active_users']}",
        f"🆕 أعضاء جدد اليوم: {today['new_users']}",
        f"👆 أشخاص ضغطوا الأزرار اليوم: {today['button_users']}",
        f"🔘 مجموع ضغطات اليوم: {today['button_clicks']}",
        f"🔍 عمليات البحث اليوم: {today['searches']}",
        f"❌ بحث بدون نتيجة اليوم: {today['empty_searches']}",
        f"🏥 الصيدليات: {stats['pharmacies']}",
        f"📅 المناوبات النشطة: {stats['shifts']}",
        f"📝 المسودات: {stats['drafts']}",
        f"⚠️ الأخطاء: {stats['errors']}",
        f"👤 الإداريون: {stats['admins']}",
        f"🚀 مرات الضغط على Start: {usage['starts']}",
        f"🔍 كل عمليات البحث: {usage['searches']}",
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
            f"🕐 أبعد مناوبة مجدولة: {format_date_ar(latest, settings.timezone)}، {format_time_ar(latest, settings.timezone)}"
        )
    await safe_edit(
        callback,
        texts.admin_section_text(
            "الإحصائيات",
            "ملخص سريع لحالة البوت وقاعدة البيانات. هذه البيانات لا تظهر للمستخدمين.",
            stats=lines,
        ),
        keyboards.admin_statistics(),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:stats:buttons:"))
async def admin_button_statistics(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    parts = (callback.data or "").split(":")
    days = _valid_days(parts[3] if len(parts) > 3 else "7")
    async with db.session_factory() as session:
        items = await repositories.button_usage_statistics(
            session,
            since=_activity_since(days, settings),
        )
        overview = await repositories.activity_overview(
            session,
            since=_activity_since(days, settings),
        )
    lines = [
        f"👥 أشخاص مختلفون: {overview['button_users']}",
        f"🔘 مجموع الضغطات: {overview['button_clicks']}",
    ]
    for item in items[:15]:
        name = BUTTON_NAMES.get(str(item["action"])) or str(item["button_text"] or item["action"])
        lines.append(f"• {html(name)}: {item['users']} شخص — {item['clicks']} ضغطة")
    if not items:
        lines.append("ℹ️ لا توجد ضغطات مسجلة في هذه الفترة.")
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"تفاعل الأزرار — {_period_name(days)}",
            "يعرض عدد الأشخاص المختلفين وإجمالي الضغطات على أزرار المستخدمين. يبدأ السجل التفصيلي من نشر هذا التحديث.",
            stats=lines,
        ),
        keyboards.statistics_periods("buttons", days),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:stats:active:"))
async def admin_active_users(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    parts = (callback.data or "").split(":")
    days = _valid_days(parts[3] if len(parts) > 3 else "7")
    try:
        page = max(0, int(parts[4] if len(parts) > 4 else "0"))
    except ValueError:
        page = 0
    async with db.session_factory() as session:
        users, total = await repositories.list_active_users(
            session,
            since=_activity_since(days, settings),
            limit=ACTIVE_USERS_PAGE_SIZE,
            offset=page * ACTIVE_USERS_PAGE_SIZE,
        )
    if page and not users:
        page = 0
        async with db.session_factory() as session:
            users, total = await repositories.list_active_users(
                session,
                since=_activity_since(days, settings),
                limit=ACTIVE_USERS_PAGE_SIZE,
                offset=0,
            )
    lines = [f"👥 العدد: {total}"]
    for index, item in enumerate(users, start=page * ACTIVE_USERS_PAGE_SIZE + 1):
        name = " ".join(
            part for part in (str(item.get("first_name") or ""), str(item.get("last_name") or "")) if part
        ).strip() or str(item.get("username") or item["telegram_id"])
        last_seen = format_datetime_ar(item["last_seen_at"], settings.timezone)
        lines.append(
            f"{index}. {html(name)} — {last_seen}\n"
            f"   👆 {item['buttons']} ضغطات • 💬 {item['messages']} رسائل • 🔍 {item['searches']} بحث"
        )
    if not users:
        lines.append("ℹ️ لا يوجد أعضاء نشطون في هذه الفترة.")
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"الأعضاء النشطون — {_period_name(days)}",
            "النشط هو من تفاعل مع البوت خلال الفترة. اضغط على اسمه لفتح سجله.",
            stats=lines,
        ),
        keyboards.active_users_statistics(
            users,
            selected_days=days,
            page=page,
            total=total,
            page_size=ACTIVE_USERS_PAGE_SIZE,
        ),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("a:stats:user:"))
async def admin_user_activity(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    parts = (callback.data or "").split(":")
    try:
        user_id = int(parts[3])
        days = _valid_days(parts[4])
        page = max(0, int(parts[5]))
    except (IndexError, ValueError):
        await answer_callback(callback, "تعذر فتح سجل العضو.", alert=True)
        return
    async with db.session_factory() as session:
        user, summary, events = await repositories.user_activity_details(
            session,
            user_id,
            since=_activity_since(days, settings),
        )
    if user is None:
        await answer_callback(callback, "العضو غير موجود.", alert=True)
        return
    username = f"@{html(user.username)}" if user.username else "غير موجود"
    lines = [
        f"🪪 الاسم: {html(_user_name(user))}",
        f"🔗 المعرف: {username}",
        f"🆔 Telegram ID: <code>{user.telegram_id}</code>",
        f"📅 أول استخدام: {format_datetime_ar(user.first_seen_at, settings.timezone)}",
        f"🕐 آخر نشاط: {format_datetime_ar(user.last_seen_at, settings.timezone)}",
        f"🚀 مرات Start: {user.start_count}",
        f"👆 الضغطات: {summary['buttons']}",
        f"💬 الرسائل: {summary['messages']}",
        f"🔍 عمليات البحث: {summary['searches']}",
    ]
    if events:
        lines.append("\n🧾 آخر العمليات:")
        for event in events:
            local = as_local(event.created_at, settings.timezone)
            lines.append(f"• {local:%d/%m %H:%M} — {html(_activity_event_name(event))}")
    else:
        lines.append("\nℹ️ لا توجد عمليات تفصيلية مسجلة في هذه الفترة.")
    await safe_edit(
        callback,
        texts.admin_section_text(
            f"سجل العضو — {_period_name(days)}",
            "هذا السجل ظاهر للإدارة فقط.",
            stats=lines,
        ),
        keyboards.user_activity_statistics(selected_days=days, page=page),
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
