from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import callbacks as cb, public_keyboards, texts
from app.models import Pharmacy, Shift


def _current_shift() -> Shift:
    pharmacy = Pharmacy(
        id=10,
        name="صيدلية النور",
        normalized_name="النور",
        address="شارع البلدية",
        status="active",
    )
    shift = Shift(
        id=20,
        pharmacy_id=pharmacy.id,
        start_at=datetime(2026, 8, 5, 10, 30, tzinfo=timezone.utc),
        end_at=datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc),
        active=True,
    )
    shift.pharmacy = pharmacy
    return shift


def test_home_shows_current_pharmacy_instead_of_last_update() -> None:
    now = datetime(2026, 8, 5, 11, 2, tzinfo=timezone.utc)
    text = texts.user_home_text(now, ZoneInfo("Asia/Damascus"), [_current_shift()])

    assert "الصيدلية المناوبة الآن" in text
    assert "صيدلية النور" in text
    assert "مناوبة نهارية" in text
    assert "1:30 مساءً" in text
    assert "5:00 مساءً" in text
    assert "آخر تحديث" not in text


def test_home_handles_no_current_pharmacy() -> None:
    now = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
    text = texts.user_home_text(now, ZoneInfo("Asia/Damascus"), [])

    assert "لا توجد صيدلية مناوبة الآن" in text
    assert "آخر تحديث" not in text


def test_home_refresh_button_uses_current_status_callback() -> None:
    markup = public_keyboards.user_home()
    buttons = [button for row in markup.inline_keyboard for button in row]
    refresh = next(button for button in buttons if button.callback_data == cb.USER_REFRESH)

    assert refresh.text == "🔄 تحديث الوقت"
