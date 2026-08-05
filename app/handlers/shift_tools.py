"""Extra shift-management tools attached to the existing shifts router."""

from app import callbacks as cb, keyboards


def _extended_admin_shifts_keyboard():
    return keyboards.keyboard(
        [
            [keyboards.button("🌙 المناوبات الحالية", cb.ADMIN_SHIFT_NOW, "primary")],
            [
                keyboards.button("📅 مناوبات اليوم", cb.ADMIN_SHIFT_TODAY),
                keyboards.button("⏭ مناوبات غداً", cb.ADMIN_SHIFT_TOMORROW),
            ],
            [keyboards.button("📆 المناوبات القادمة", cb.ADMIN_SHIFT_UPCOMING)],
            [
                keyboards.button("🔍 البحث", cb.ADMIN_SHIFT_SEARCH),
                keyboards.button("➕ إضافة مناوبة", cb.ADMIN_SHIFT_ADD, "success"),
            ],
            [keyboards.button("🔁 تبديل الصيدلية المناوبة", cb.ADMIN_SHIFT_SWAP, "success")],
            [keyboards.button("🕐 تعديل أوقات المناوبات العامة", cb.ADMIN_SHIFT_GLOBAL_TIMES, "primary")],
            [keyboards.button("🗑 حذف فترة", cb.ADMIN_SHIFT_DELETE_PERIOD, "danger")],
            [keyboards.button("⚠️ فحص المناوبات", cb.ADMIN_SHIFT_CHECK)],
            [keyboards.button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def _extended_exports_keyboard():
    return keyboards.keyboard(
        [
            [keyboards.button("📥 تصدير الصيدليات Excel", cb.ADMIN_EXPORT_PHARMACIES, "success")],
            [keyboards.button("📥 تصدير المناوبات Excel", cb.ADMIN_EXPORT_SHIFTS, "success")],
            [keyboards.button("📄 تصدير المناوبات Word", cb.ADMIN_EXPORT_WORD, "success")],
            [keyboards.button("📦 نسخة احتياطية JSON", cb.ADMIN_EXPORT_JSON)],
            [keyboards.button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


# Keep the established menus intact and append only the approved options.
keyboards.admin_shifts = _extended_admin_shifts_keyboard
keyboards.exports = _extended_exports_keyboard

# Import for handler-registration side effects after the menu patch is active.
from . import shift_swap as _shift_swap  # noqa: E402,F401
from . import shift_time_settings as _shift_time_settings  # noqa: E402,F401
