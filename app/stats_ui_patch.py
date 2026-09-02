from __future__ import annotations

"""Small UI overrides for the admin statistics screens.

The active-users keyboard is intentionally left to the original implementation
in app.keyboards so the proven callback flow remains stable.
"""

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardMarkup

from app import callbacks as cb, keyboards


def admin_statistics() -> InlineKeyboardMarkup:
    return keyboards.keyboard(
        [
            [
                keyboards.button("👥 الأعضاء النشطون", "a:stats:active:7:0", ButtonStyle.PRIMARY),
                keyboards.button("👆 ضغطات الأزرار", "a:stats:buttons:7", ButtonStyle.PRIMARY),
            ],
            [keyboards.button("🔄 تحديث الإحصائيات", cb.ADMIN_STATS, ButtonStyle.SUCCESS)],
            [keyboards.button("⬅️ رجوع", cb.ADMIN_HOME)],
        ]
    )


def install() -> None:
    # Do not replace active_users_statistics here. The original keyboard uses
    # callback buttons for each user and is the known-good implementation.
    keyboards.admin_statistics = admin_statistics
