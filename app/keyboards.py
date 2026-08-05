from __future__ import annotations

from collections.abc import Iterable

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import callbacks as cb
from app.models import Admin, ImportBatch, Pharmacy, Shift


def button(text: str, callback_data: str, style: ButtonStyle | str | None = None) -> InlineKeyboardButton:
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data is longer than 64 bytes: {callback_data}")
    if style is None and (text.startswith("⬅️") or "الرجوع" in text):
        style = ButtonStyle.PRIMARY
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


def keyboard(rows: Iterable[Iterable[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def user_home(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [button("🌙 الصيدليات المناوبة الآن", cb.USER_NOW, ButtonStyle.PRIMARY)],
        [
            button("📅 صيدليات اليوم", cb.USER_TODAY),
            button("⏭ صيدليات غداً", cb.USER_TOMORROW),
        ],
        [button("🔍 البحث عن صيدلية", cb.USER_SEARCH, ButtonStyle.PRIMARY)],
        [button("🔄 تحديث الوقت", cb.USER_REFRESH, ButtonStyle.SUCCESS)],
    ]
    if is_admin:
        rows.append([button("⚙️ لوحة الإدارة", cb.ADMIN_HOME, ButtonStyle.PRIMARY)])
    return keyboard(rows)


def user_results() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("🔄 تحديث", cb.USER_REFRESH, ButtonStyle.SUCCESS)],
            [
                button("📅 اليوم", cb.USER_TODAY),
                button("⏭ غداً", cb.USER_TOMORROW),
            ],
            [button("🔍 بحث جديد", cb.USER_SEARCH, ButtonStyle.PRIMARY)],
            [button("⬅️ رجوع", cb.USER_HOME)],
        ]
    )


def back_user() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("🔍 بحث جديد", cb.USER_SEARCH, ButtonStyle.PRIMARY)],
            [button("⬅️ رجوع", cb.USER_HOME)],
        ]
    )


def admin_home() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✨ إدخال جدول جديد", cb.ADMIN_IMPORT, ButtonStyle.PRIMARY)],
            [
                button("📷 قراءة صورة بـ GPT-5.4 Mini", cb.ADMIN_IMPORT_GEMINI, ButtonStyle.PRIMARY),
                button("📊 رفع ملف Excel", cb.ADMIN_IMPORT_EXCEL, ButtonStyle.PRIMARY),
            ],
            [button("📄 رفع جدول Word", cb.ADMIN_IMPORT_WORD, ButtonStyle.SUCCESS)],
            [button("📝 المسودات", cb.ADMIN_DRAFTS)],
            [
                button("📅 إدارة المناوبات", cb.ADMIN_SHIFTS, ButtonStyle.PRIMARY),
                button("🏥 إدارة الصيدليات", cb.ADMIN_PHARMACIES, ButtonStyle.PRIMARY),
            ],
            [button("⚠️ الأخطاء والتنبيهات", cb.ADMIN_ERRORS)],
            [
                button("↩️ التراجع", cb.ADMIN_UNDO),
                button("👁 معاينة كمستخدم", cb.ADMIN_PREVIEW),
            ],
            [button("📥 النسخ والتصدير", cb.ADMIN_EXPORTS)],
            [
                button("📊 الإحصائيات", cb.ADMIN_STATS),
                button("👥 إدارة الأدمن", cb.ADMIN_ADMINS),
            ],
            [button("🔔 إشعارات الدخول", cb.ADMIN_ENTRY_NOTIFICATIONS)],
            [button("⬅️ الرجوع للبوت", cb.ADMIN_BACK_USER)],
        ]
    )


def admin_import() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("📄 رفع جدول Word الرسمي", cb.ADMIN_IMPORT_WORD, ButtonStyle.SUCCESS)],
            [button("📷 قراءة صورة بواسطة GPT-5.4 Mini", cb.ADMIN_IMPORT_GEMINI, ButtonStyle.PRIMARY)],
            [button("📊 رفع ملف Excel", cb.ADMIN_IMPORT_EXCEL, ButtonStyle.PRIMARY)],
            [button("✍️ إضافة مناوبة يدوياً", cb.ADMIN_IMPORT_MANUAL, ButtonStyle.SUCCESS)],
            [button("📥 تنزيل نموذج Excel", cb.ADMIN_TEMPLATE_SHIFTS)],
            [button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def admin_shifts() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("🌙 المناوبات الحالية", cb.ADMIN_SHIFT_NOW, ButtonStyle.PRIMARY)],
            [
                button("📅 مناوبات اليوم", cb.ADMIN_SHIFT_TODAY),
                button("⏭ مناوبات غداً", cb.ADMIN_SHIFT_TOMORROW),
            ],
            [button("📆 المناوبات القادمة", cb.ADMIN_SHIFT_UPCOMING)],
            [
                button("🔍 البحث", cb.ADMIN_SHIFT_SEARCH),
                button("➕ إضافة مناوبة", cb.ADMIN_SHIFT_ADD, ButtonStyle.SUCCESS),
            ],
            [button("🗑 حذف فترة", cb.ADMIN_SHIFT_DELETE_PERIOD, ButtonStyle.DANGER)],
            [button("⚠️ فحص المناوبات", cb.ADMIN_SHIFT_CHECK)],
            [button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def shift_list(shifts: list[Shift], back_callback: str = cb.ADMIN_SHIFTS) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for shift in shifts[:20]:
        rows.append([button(f"💊 {shift.pharmacy.name}", f"a:s:view:{shift.id}")])
    rows.append([button("➕ إضافة مناوبة", cb.ADMIN_SHIFT_ADD, ButtonStyle.SUCCESS)])
    rows.append([button("⬅️ رجوع", back_callback)])
    return keyboard(rows)


def shift_detail(shift_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button("🏥 تغيير الصيدلية", f"a:s:edit:{shift_id}:pharmacy"),
                button("📅 تعديل التاريخ والوقت", f"a:s:edit:{shift_id}:datetime"),
            ],
            [button("📋 نسخ المناوبة", f"a:s:copy:{shift_id}")],
            [button("🗑 حذف المناوبة", f"a:s:delete_ask:{shift_id}", ButtonStyle.DANGER)],
            [button("⬅️ رجوع", cb.ADMIN_SHIFTS)],
        ]
    )


def confirm_shift_delete(shift_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✅ تأكيد الحذف", f"a:s:delete:{shift_id}", ButtonStyle.DANGER)],
            [button("❌ إلغاء", f"a:s:view:{shift_id}")],
        ]
    )


def admin_pharmacies() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("➕ إضافة صيدلية", cb.ADMIN_PHARMACY_ADD, ButtonStyle.SUCCESS)],
            [
                button("🔍 البحث عن صيدلية", cb.ADMIN_PHARMACY_SEARCH),
                button("📋 جميع الصيدليات", cb.ADMIN_PHARMACY_LIST),
            ],
            [button("⚠️ بيانات ناقصة", cb.ADMIN_PHARMACY_INCOMPLETE)],
            [button("📊 استيراد الصيدليات من Excel", cb.ADMIN_PHARMACY_IMPORT, ButtonStyle.PRIMARY)],
            [button("📥 تنزيل نموذج الصيدليات", cb.ADMIN_TEMPLATE_PHARMACIES)],
            [button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def pharmacy_list(pharmacies: list[Pharmacy]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pharmacy in pharmacies[:100]:
        icon = "⚠️" if not pharmacy.address.strip() else "💊"
        rows.append([button(f"{icon} {pharmacy.name}", f"a:p:view:{pharmacy.id}")])
    rows.append([button("➕ إضافة صيدلية", cb.ADMIN_PHARMACY_ADD, ButtonStyle.SUCCESS)])
    rows.append([button("⬅️ رجوع", cb.ADMIN_PHARMACIES)])
    return keyboard(rows)


def pharmacy_detail(pharmacy_id: int, status: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button("✏️ تعديل الاسم", f"a:p:edit:{pharmacy_id}:name"),
                button("📍 تعديل العنوان", f"a:p:edit:{pharmacy_id}:address"),
            ],
            [
                button("🔤 تعديل الأسماء البديلة", f"a:p:edit:{pharmacy_id}:aliases"),
                button("📝 تعديل الملاحظات", f"a:p:edit:{pharmacy_id}:notes"),
            ],
            [button("📌 تعديل الحالة", f"a:p:status_menu:{pharmacy_id}", ButtonStyle.PRIMARY)],
            [button("🗑 حذف الصيدلية", f"a:p:delete_ask:{pharmacy_id}", ButtonStyle.DANGER)],
            [button("⬅️ رجوع", cb.ADMIN_PHARMACIES)],
        ]
    )


def pharmacy_status(pharmacy_id: int, current: str) -> InlineKeyboardMarkup:
    def label(status: str, text: str) -> str:
        return f"✅ {text}" if current == status else text

    return keyboard(
        [
            [button(label("active", "🟢 فعالة"), f"a:p:status:{pharmacy_id}:active", ButtonStyle.SUCCESS)],
            [button(label("temporarily_closed", "⏸ مغلقة مؤقتاً"), f"a:p:status:{pharmacy_id}:temporarily_closed")],
            [button(label("inactive", "🚫 متوقفة"), f"a:p:status:{pharmacy_id}:inactive", ButtonStyle.DANGER)],
            [button("⬅️ رجوع", f"a:p:view:{pharmacy_id}")],
        ]
    )

def confirm_pharmacy_delete(pharmacy_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✅ تأكيد الحذف", f"a:p:delete:{pharmacy_id}", ButtonStyle.DANGER)],
            [button("❌ إلغاء", f"a:p:view:{pharmacy_id}")],
        ]
    )


def drafts(batches: list[ImportBatch]) -> InlineKeyboardMarkup:
    rows = [[button(f"📝 مسودة #{batch.id}", f"a:d:view:{batch.id}")] for batch in batches]
    rows.append([button("✨ إدخال جدول جديد", cb.ADMIN_IMPORT, ButtonStyle.PRIMARY)])
    rows.append([button("⬅️ رجوع", cb.ADMIN_HOME)])
    return keyboard(rows)


def draft_detail(batch_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                button("⚠️ مراجعة الأخطاء", f"a:d:errors:{batch_id}"),
                button("👁 معاينة كاملة", f"a:d:preview:{batch_id}"),
            ],
            [
                button(
                    "🏥 حفظ أسماء الصيدليات تلقائياً",
                    f"a:d:auto_pharmacies:{batch_id}",
                    ButtonStyle.SUCCESS,
                )
            ],
            [
                button(
                    "📊 إضافة الصيدليات والعناوين عبر Excel",
                    f"a:d:missing:{batch_id}",
                    ButtonStyle.PRIMARY,
                )
            ],
            [button("✅ إضافة ونشر", f"a:d:publish_ask:{batch_id}:add", ButtonStyle.SUCCESS)],
            [button("🔄 استبدال الفترة ونشر", f"a:d:publish_ask:{batch_id}:replace", ButtonStyle.DANGER)],
            [button("🗑 حذف المسودة", f"a:d:cancel_ask:{batch_id}", ButtonStyle.DANGER)],
            [button("⬅️ رجوع", cb.ADMIN_DRAFTS)],
        ]
    )

def confirm_publish(batch_id: int, mode: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✅ تأكيد النشر", f"a:d:publish:{batch_id}:{mode}", ButtonStyle.SUCCESS)],
            [button("❌ إلغاء", f"a:d:view:{batch_id}")],
        ]
    )


def confirm_cancel_batch(batch_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✅ حذف المسودة", f"a:d:cancel:{batch_id}", ButtonStyle.DANGER)],
            [button("❌ إلغاء", f"a:d:view:{batch_id}")],
        ]
    )


def exports() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("📥 تصدير الصيدليات Excel", cb.ADMIN_EXPORT_PHARMACIES, ButtonStyle.SUCCESS)],
            [button("📥 تصدير المناوبات Excel", cb.ADMIN_EXPORT_SHIFTS, ButtonStyle.SUCCESS)],
            [button("📦 نسخة احتياطية JSON", cb.ADMIN_EXPORT_JSON)],
            [button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def admins(admins_list: list[Admin], owner: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if owner:
        rows.append([button("➕ إضافة أدمن", cb.ADMIN_ADMIN_ADD, ButtonStyle.SUCCESS)])
    rows.append([button("📋 قائمة الأدمن", cb.ADMIN_ADMIN_LIST)])
    rows.append([button("🔔 إعداد إشعارات الدخول", cb.ADMIN_ADMIN_TOGGLE_NOTIFY)])
    rows.append([button("⬅️ رجوع", cb.ADMIN_HOME)])
    return keyboard(rows)


def admin_list(admins_list: list[Admin], owner: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for admin in admins_list:
        label = f"{'✅' if admin.active else '🚫'} {admin.telegram_id} — {admin.role}"
        if owner and admin.role != "owner":
            rows.append([button(label, f"a:m:remove_ask:{admin.telegram_id}")])
        else:
            rows.append([button(label, cb.ADMIN_ADMINS)])
    rows.append([button("⬅️ رجوع", cb.ADMIN_ADMINS)])
    return keyboard(rows)


def confirm_admin_remove(telegram_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [button("✅ حذف الأدمن", f"a:m:remove:{telegram_id}", ButtonStyle.DANGER)],
            [button("❌ إلغاء", cb.ADMIN_ADMIN_LIST)],
        ]
    )


def notifications(enabled: bool) -> InlineKeyboardMarkup:
    label = "🔕 إيقاف إشعارات الدخول" if enabled else "🔔 تشغيل إشعارات الدخول"
    style = ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS
    return keyboard(
        [
            [button(label, "a:n:toggle", style)],
            [button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def simple_back(callback_data: str = cb.ADMIN_HOME) -> InlineKeyboardMarkup:
    return keyboard([[button("⬅️ رجوع", callback_data)]])
