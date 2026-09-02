from __future__ import annotations

"""Small UI overrides for the admin statistics screens.

Kept isolated so the previous statistics implementation can be restored easily.
"""

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import callbacks as cb, keyboards


def active_users_statistics(
    users: list[dict[str, object]],
    *,
    selected_days: int,
    page: int,
    total: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            keyboards.button(
                f"{'✅ ' if selected_days == days else ''}{label}",
                f"a:stats:active:{days}:0",
            )
            for label, days in (("اليوم", 1), ("7 أيام", 7), ("30 يومًا", 30))
        ],
        [
            keyboards.button(
                f"{'✅ ' if selected_days == 0 else ''}كل الوقت",
                "a:stats:active:0:0",
            )
        ],
    ]

    for item in users:
        name = " ".join(
            part
            for part in (
                str(item.get("first_name") or ""),
                str(item.get("last_name") or ""),
            )
            if part
        ).strip()
        username = str(item.get("username") or "").strip().lstrip("@")
        if not name:
            name = username or str(item["telegram_id"])

        # Prefer a normal Telegram username link when one exists. For users
        # without a public username, fall back to Telegram's numeric ID link.
        profile_url = (
            f"https://t.me/{username}"
            if username
            else f"tg://user?id={int(item['telegram_id'])}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {name[:36]}",
                    url=profile_url,
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(keyboards.button("◀️ السابق", f"a:stats:active:{selected_days}:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(keyboards.button("التالي ▶️", f"a:stats:active:{selected_days}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع للإحصائيات", cb.ADMIN_STATS)])
    return keyboards.keyboard(rows)


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
    keyboards.active_users_statistics = active_users_statistics
    keyboards.admin_statistics = admin_statistics
