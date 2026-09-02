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
        if not name:
            name = str(item.get("username") or item["telegram_id"])

        # URL buttons do not support Telegram's style field. Supplying both can
        # make send/editMessageReplyMarkup fail and prevents the active-users
        # screen from opening.
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {name[:36]}",
                    url=f"tg://user?id={int(item['telegram_id'])}",
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
