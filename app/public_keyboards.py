from __future__ import annotations

import os
from collections.abc import Iterable

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import callbacks as cb
from app.keyboards import button, keyboard
from app.models import Shift


DEVELOPER_URL = "https://t.me/md17l"


def _developer_button(premium_emoji_id: str | None = None) -> InlineKeyboardButton:
    configured_emoji_id = (
        premium_emoji_id or os.getenv("PREMIUM_EMOJI_ID", "")
    ).strip() or None
    label = "مطوّر البوت" if configured_emoji_id else "👨‍💻 مطوّر البوت"
    return InlineKeyboardButton(
        text=label,
        url=DEVELOPER_URL,
        style=ButtonStyle.SUCCESS,
        icon_custom_emoji_id=configured_emoji_id,
    )


def user_home(
    is_admin: bool = False,
    premium_emoji_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Public home keyboard with the requested button colors."""
    rows = [
        [button("🌙 الصيدليات المناوبة الآن", cb.USER_NOW, ButtonStyle.PRIMARY)],
        [
            button("📅 صيدليات اليوم", cb.USER_TODAY, ButtonStyle.SUCCESS),
            button("⏭ صيدليات غداً", cb.USER_TOMORROW, ButtonStyle.SUCCESS),
        ],
        [button("🔍 البحث عن صيدلية", cb.USER_SEARCH, ButtonStyle.PRIMARY)],
        [button("🔄 تحديث الوقت", cb.USER_REFRESH, ButtonStyle.DANGER)],
    ]
    if is_admin:
        rows.append([button("⚙️ لوحة الإدارة", cb.ADMIN_HOME, ButtonStyle.PRIMARY)])
    rows.append([_developer_button(premium_emoji_id)])
    return keyboard(rows)


def user_results(
    shifts: Iterable[Shift],
    *,
    refresh_callback: str,
) -> InlineKeyboardMarkup:
    """Show pharmacy names, contextual refresh, and a clear back button."""
    rows = []
    seen_pharmacy_ids: set[int] = set()

    for shift in shifts:
        pharmacy = shift.pharmacy
        if pharmacy.id in seen_pharmacy_ids:
            continue
        seen_pharmacy_ids.add(pharmacy.id)
        rows.append(
            [
                button(
                    f"💊 {pharmacy.name}",
                    f"u:pinfo:{pharmacy.id}",
                    ButtonStyle.PRIMARY,
                )
            ]
        )
        if len(seen_pharmacy_ids) >= 20:
            break

    rows.append([button("🔄 تحديث الوقت", refresh_callback, ButtonStyle.DANGER)])
    rows.append([button("⬅️ رجوع", cb.USER_HOME, ButtonStyle.PRIMARY)])
    return keyboard(rows)
