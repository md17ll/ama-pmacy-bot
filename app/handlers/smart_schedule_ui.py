from __future__ import annotations

import re

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app import keyboards, texts
from app.handlers import smart_schedules as smart
from app.handlers.admin import router
from app.handlers.common import require_admin


_ORIGINAL_SAFE_EDIT = smart.safe_edit
_ORIGINAL_ANSWER_CALLBACK = smart.answer_callback


def _callback_data(markup: InlineKeyboardMarkup | None, prefix: str) -> str | None:
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            value = button.callback_data
            if value and value.startswith(prefix):
                return value
    return None


def _line(text: str, marker: str) -> str | None:
    return next((line for line in text.splitlines() if marker in line), None)


def _without_help(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.startswith("🔹 ")]
    while len(lines) >= 2 and not lines[-2].strip():
        lines.pop(-2)
    return "\n".join(lines)


def _home_ui(text: str, markup: InlineKeyboardMarkup | None):
    create_cb = _callback_data(markup, "a:smart:new") or "a:smart:new"
    draft_cb = _callback_data(markup, "a:smart:draft:")
    history_cb = _callback_data(markup, "a:smart:history:") or "a:smart:history:0"

    stats = []
    for marker in ("📋 آخر يوم منشور:", "⏭️ بداية الجدول التالي:", "⏭️ سيبدأ الجدول", "📚 الجداول الذكية المنشورة:"):
        found = _line(text, marker)
        if found and found not in stats:
            stats.append(found)
    stats.append("💡 يمكنك مراجعة الجدول وتعديله قبل اعتماده ونشره.")

    rows = [[keyboards.button("✨ إنشاء جدول جديد", create_cb, ButtonStyle.SUCCESS)]]
    if draft_cb:
        rows.append([keyboards.button("📝 المسودة الحالية", draft_cb, ButtonStyle.PRIMARY)])
    rows.extend(
        [
            [keyboards.button("📚 الجداول السابقة", history_cb, ButtonStyle.PRIMARY)],
            [keyboards.button("⚙️ خيارات متقدمة", "a:smart:advanced:home", ButtonStyle.PRIMARY)],
            [keyboards.button("⬅️ رجوع", smart.cb.ADMIN_HOME)],
        ]
    )
    return (
        texts.admin_section_text(
            "مولّد الجداول الذكي",
            "أنشئ جدول المناوبات تلقائياً بتوزيع متوازن بين الصيدليات.",
            stats=stats,
        ),
        keyboards.keyboard(rows),
    )


def _range_ui(text: str, markup: InlineKeyboardMarkup | None):
    start = _line(text, "📅 البداية:")
    end = _line(text, "📅 النهاية:")
    days = _line(text, "🗓️ عدد الأيام:")
    stats = [item for item in (start, end, days) if item]
    stats.append("🔒 لن يتم نشر أي شيء قبل موافقتك.")

    minus_cb = _callback_data(markup, "a:smart:range:")
    range_callbacks: list[str] = []
    if markup:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data and button.callback_data.startswith("a:smart:range:"):
                    range_callbacks.append(button.callback_data)
    generate_cb = _callback_data(markup, "a:smart:generate:")

    rows = []
    if len(range_callbacks) >= 2:
        rows.append(
            [
                keyboards.button("➖ يوم", range_callbacks[0]),
                keyboards.button("➕ يوم", range_callbacks[1]),
            ]
        )
    elif minus_cb:
        rows.append([keyboards.button("➖ / ➕ تعديل الفترة", minus_cb)])
    if generate_cb:
        rows.append([keyboards.button("✅ إنشاء الجدول", generate_cb, ButtonStyle.SUCCESS)])
    rows.append([keyboards.button("⬅️ رجوع", smart.SMART_HOME)])

    return (
        texts.admin_section_text(
            "فترة الجدول",
            "راجع تاريخ البداية والنهاية، ثم أنشئ الجدول.",
            stats=stats,
        ),
        keyboards.keyboard(rows),
    )


def _draft_ui(text: str, markup: InlineKeyboardMarkup | None):
    view_cb = _callback_data(markup, "a:smart:view:")
    edit_cb = _callback_data(markup, "a:smart:edit:")
    word_cb = _callback_data(markup, "a:smart:word:")
    publish_cb = _callback_data(markup, "a:smart:publishask:")
    delete_cb = _callback_data(markup, "a:smart:deleteask:")

    batch_id = None
    if view_cb:
        parts = view_cb.split(":")
        if len(parts) >= 4:
            batch_id = parts[3]

    period = _line(text, "📅 الفترة:")
    rating = _line(text, "⚖️ تقييم العدالة:")
    total = _line(text, "📋 المناوبات:")
    hard = _line(text, "⛔ تعارضات صلبة:")
    stats = [item for item in (period, total) if item]
    if rating:
        stats.append(rating.replace("⚖️ تقييم العدالة:", "⚖️ حالة التوزيع:"))
    hard_count = None
    if hard:
        match = re.search(r"(\d+)\s*$", hard)
        hard_count = int(match.group(1)) if match else None
    if hard_count == 0:
        stats.append("✅ لا توجد مشاكل تمنع النشر.")
    elif hard_count:
        stats.append(f"⚠️ توجد {hard_count} مشكلة تحتاج إلى تعديل قبل النشر.")
    stats.append("📝 الجدول ما زال غير منشور.")

    rows = []
    if view_cb:
        rows.append([keyboards.button("👁 عرض الجدول", view_cb, ButtonStyle.PRIMARY)])
    if edit_cb:
        rows.append([keyboards.button("✏️ تعديل الجدول", edit_cb, ButtonStyle.PRIMARY)])
    if word_cb:
        rows.append([keyboards.button("📄 تحميل Word", word_cb, ButtonStyle.PRIMARY)])
    if publish_cb:
        rows.append([keyboards.button("✅ اعتماد ونشر", publish_cb, ButtonStyle.SUCCESS)])
    if batch_id:
        rows.append([keyboards.button("⚙️ خيارات متقدمة", f"a:smart:advanced:draft:{batch_id}", ButtonStyle.PRIMARY)])
    if delete_cb:
        rows.append([keyboards.button("🗑 حذف المسودة", delete_cb, ButtonStyle.DANGER)])
    rows.append([keyboards.button("⬅️ رجوع", smart.SMART_HOME)])

    return (
        texts.admin_section_text(
            "المسودة الحالية",
            "راجع الجدول وعدّله إذا لزم، ثم اعتمده عندما تتأكد منه.",
            stats=stats,
        ),
        keyboards.keyboard(rows),
    )


def _choose_ui(text: str, markup: InlineKeyboardMarkup | None):
    if markup is None:
        return _without_help(text), markup
    rows = []
    rank = 0
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            cb = button.callback_data or ""
            if cb.startswith("a:smart:pick:"):
                rank += 1
                name = button.text.split(" • ", 1)[0]
                label = f"⭐ {name}" if rank <= 3 else name
                new_row.append(keyboards.button(label, cb, ButtonStyle.PRIMARY))
            else:
                new_row.append(button)
        rows.append(new_row)
    simplified = texts.admin_section_text(
        "اختيار صيدلية بديلة",
        "الصيدليات مرتبة من الأنسب إلى الأقل. اختر الصيدلية المطلوبة.",
    )
    return simplified, keyboards.keyboard(rows)


def _analysis_ui(text: str, markup: InlineKeyboardMarkup | None):
    values: dict[str, int] = {}
    markers = {
        "total": "📏 فرق الإجمالي:",
        "day": "☀️ فرق النهاري:",
        "evening": "🌙 فرق الليلي:",
        "consecutive": "🔁 يومان متتاليان:",
        "same_day": "⛔ نفس الصيدلية في اليوم:",
        "friday_over": "🕌 تجاوز 2/2:",
        "friday_priority": "🎯 مخالفة أولوية 0/2 ثم 1/2:",
    }
    for key, marker in markers.items():
        line = _line(text, marker)
        if line:
            match = re.search(r"(-?\d+)\s*$", line)
            if match:
                values[key] = int(match.group(1))
    rating = _line(text, "⚖️ التقييم:")
    stats = [rating] if rating else []
    spread = values.get("total")
    stats.append("⚖️ التوزيع العام: ✅ متوازن" if spread is not None and spread <= 1 else f"⚖️ التوزيع العام: ⚠️ فرق {spread}" if spread is not None else "⚖️ التوزيع العام: راجع التفاصيل")
    for label, key in (("☀️ المناوبات النهارية", "day"), ("🌙 المناوبات الليلية", "evening")):
        value = values.get(key)
        stats.append(f"{label}: ✅ متوازنة" if value is not None and value <= 1 else f"{label}: ⚠️ فرق {value}" if value is not None else f"{label}: غير محدد")
    consecutive = values.get("consecutive")
    stats.append("🔁 المناوبات المتتالية: ✅ لا يوجد" if consecutive == 0 else f"🔁 المناوبات المتتالية: ⚠️ يوجد {consecutive}" if consecutive is not None else "🔁 المناوبات المتتالية: غير محدد")
    same_day = values.get("same_day")
    friday_over = values.get("friday_over")
    friday_priority = values.get("friday_priority")
    problems = sum(value for value in (same_day, friday_over, friday_priority) if value is not None)
    stats.append("⛔ المشاكل المانعة للنشر: ✅ لا يوجد" if problems == 0 else f"⛔ المشاكل المانعة للنشر: ⚠️ {problems}")

    return (
        texts.admin_section_text(
            "تحليل الجدول",
            "ملخص سريع لحالة توزيع المناوبات في المسودة الحالية.",
            stats=stats,
        ),
        markup,
    )


def _publish_ask_ui(text: str, markup: InlineKeyboardMarkup | None):
    period = _line(text, "📅 ")
    rating = _line(text, "⚖️ التقييم:")
    stats = [item for item in (period, rating) if item]
    stats.append("✅ الجدول جاهز للنشر بعد تأكيدك.")
    if markup is None:
        return text, markup
    publish_cb = _callback_data(markup, "a:smart:publish:")
    draft_cb = _callback_data(markup, "a:smart:draft:")
    rows = []
    if publish_cb:
        rows.append([keyboards.button("✅ تأكيد ونشر الجدول", publish_cb, ButtonStyle.SUCCESS)])
    if draft_cb:
        rows.append([keyboards.button("👁 رجوع للمراجعة", draft_cb, ButtonStyle.PRIMARY)])
    return (
        texts.admin_section_text(
            "اعتماد الجدول",
            "بعد التأكيد سيصبح الجدول منشوراً ومتاحاً للمستخدمين.",
            stats=stats,
        ),
        keyboards.keyboard(rows),
    )


def _published_ui(text: str, markup: InlineKeyboardMarkup | None):
    stats = [line for line in text.splitlines() if line.startswith("✅ مناوبات منشورة:") or line.startswith("🗑️ مستبدلة:")]
    return (
        texts.admin_section_text(
            "تم اعتماد الجدول",
            "✅ أصبح الجدول منشوراً ومتاحاً للمستخدمين.",
            stats=stats,
        ),
        markup,
    )


async def _simple_safe_edit(target, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    data = getattr(target, "data", "") or ""

    if "لوحة الإدارة › مولّد الجداول الذكي" in text:
        text, reply_markup = _home_ui(text, reply_markup)
    elif "لوحة الإدارة › إنشاء جدول ذكي جديد" in text:
        text, reply_markup = _range_ui(text, reply_markup)
    elif "لوحة الإدارة › المسودة الذكية #" in text:
        text, reply_markup = _draft_ui(text, reply_markup)
    elif "لوحة الإدارة › اختيار صيدلية بديلة" in text:
        text, reply_markup = _choose_ui(text, reply_markup)
    elif "لوحة الإدارة › تحليل الجدول" in text:
        text, reply_markup = _analysis_ui(text, reply_markup)
    elif "لوحة الإدارة › إعادة التوزيع" in text:
        text = texts.admin_section_text(
            "إعادة توزيع الجدول",
            "سيحاول البوت تحسين التوزيع. الأيام المثبتة لن تتغير، وباقي الأيام قد تتغير.",
        )
        if reply_markup:
            confirm = _callback_data(reply_markup, "a:smart:reroll:")
            cancel = _callback_data(reply_markup, "a:smart:draft:")
            rows = []
            if confirm:
                rows.append([keyboards.button("✅ إعادة التوزيع", confirm, ButtonStyle.SUCCESS)])
            if cancel:
                rows.append([keyboards.button("❌ إلغاء", cancel, ButtonStyle.DANGER)])
            reply_markup = keyboards.keyboard(rows)
    elif "لوحة الإدارة › تأكيد اعتماد ونشر" in text:
        text, reply_markup = _publish_ask_ui(text, reply_markup)
    elif "لوحة الإدارة › تم اعتماد الجدول" in text:
        text, reply_markup = _published_ui(text, reply_markup)
    elif "لوحة الإدارة › حذف المسودة" in text:
        text = texts.admin_section_text(
            "حذف المسودة",
            "سيتم حذف هذه المسودة فقط ولن يتأثر أي جدول منشور.",
        )
    elif data.startswith("a:smart:view:"):
        text = _without_help(text)
    elif data.startswith("a:smart:edit:") or data.startswith("a:smart:day:"):
        text = _without_help(text)
    elif data.startswith("a:smart:history:") or data.startswith("a:smart:stats") or data == "a:smart:rules":
        text = _without_help(text)

    return await _ORIGINAL_SAFE_EDIT(target, text, reply_markup)


async def _simple_answer_callback(callback, text: str | None = None, alert: bool = False):
    replacements = {
        "تم إنشاء المسودة وتحليلها.": "✅ تم إنشاء الجدول.",
        "يوجد تعارض صلب. افتح التحليل وعدّل الجدول أولاً.": "⚠️ الجدول يحتاج تعديلاً قبل النشر. افتح تحليل الجدول لمعرفة المشكلة.",
        "أوقف النشر لأن الفحص الأخير وجد تعارضاً.": "⚠️ تم إيقاف النشر لأن الجدول يحتاج إلى تعديل.",
    }
    return await _ORIGINAL_ANSWER_CALLBACK(callback, replacements.get(text, text), alert)


@router.callback_query(F.data == "a:smart:advanced:home")
async def smart_advanced_home(callback: CallbackQuery, db) -> None:
    if await require_admin(callback, db) is None:
        return
    await _ORIGINAL_SAFE_EDIT(
        callback,
        texts.admin_section_text(
            "الخيارات المتقدمة",
            "هذه الأدوات اختيارية ولا تحتاجها عادةً لإنشاء ونشر جدول جديد.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📊 إحصائيات الصيدليات", "a:smart:stats", ButtonStyle.PRIMARY)],
                [keyboards.button("ℹ️ قواعد التوزيع", "a:smart:rules", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع", smart.SMART_HOME)],
            ]
        ),
    )
    await _ORIGINAL_ANSWER_CALLBACK(callback)


@router.callback_query(F.data.startswith("a:smart:advanced:draft:"))
async def smart_advanced_draft(callback: CallbackQuery, db) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        batch_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        await _ORIGINAL_ANSWER_CALLBACK(callback, "المسودة غير صالحة.", True)
        return
    await _ORIGINAL_SAFE_EDIT(
        callback,
        texts.admin_section_text(
            "خيارات الجدول المتقدمة",
            "استخدم هذه الأدوات فقط إذا أردت فحص التوزيع أو إعادة توليد الأجزاء غير المثبتة.",
        ),
        keyboards.keyboard(
            [
                [keyboards.button("📊 تحليل الجدول", f"a:smart:analysis:{batch_id}", ButtonStyle.PRIMARY)],
                [keyboards.button("🔄 إعادة توزيع تلقائي", f"a:smart:rerollask:{batch_id}", ButtonStyle.PRIMARY)],
                [keyboards.button("⬅️ رجوع للمسودة", f"a:smart:draft:{batch_id}")],
            ]
        ),
    )
    await _ORIGINAL_ANSWER_CALLBACK(callback)


if not getattr(smart, "_simple_ui_patched", False):
    smart.safe_edit = _simple_safe_edit
    smart.answer_callback = _simple_answer_callback
    smart._simple_ui_patched = True
