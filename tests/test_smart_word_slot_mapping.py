from __future__ import annotations

from datetime import date, datetime, time, timezone
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from docx import Document

import app.handlers  # noqa: F401 - installs the smart Word slot binding
from app.handlers import smart_schedules as smart_ui
from app.handlers import smart_schedule_word_slot_patch as _smart_schedule_word_slot_patch  # noqa: F401
from app.services.shift_schedule_tools import ShiftTimes
from app.services.word_export import build_official_word_schedule


TZ = ZoneInfo("Asia/Damascus")


def _utc(day: date, value: time) -> datetime:
    return datetime.combine(day, value, tzinfo=TZ).astimezone(timezone.utc)


def test_word_prefers_explicit_smart_slot_over_clock_time() -> None:
    duty = date(2026, 8, 18)
    shifts = [
        SimpleNamespace(
            start_at=_utc(duty, time(17, 0)),
            end_at=_utc(duty, time(20, 0)),
            active=True,
            period="evening",
            pharmacy=SimpleNamespace(name="صيدلية المسائي"),
        ),
        SimpleNamespace(
            start_at=_utc(duty, time(19, 0)),
            end_at=_utc(duty, time(22, 0)),
            active=True,
            period="day",
            pharmacy=SimpleNamespace(name="صيدلية النهاري"),
        ),
    ]

    document = Document(BytesIO(build_official_word_schedule(shifts, TZ, ShiftTimes())))
    row = document.tables[0].rows[1]
    assert row.cells[1].text == "صيدلية النهاري"
    assert row.cells[2].text == "صيدلية المسائي"


def test_smart_word_preview_uses_period_saved_on_each_draft_row() -> None:
    duty = date(2026, 8, 18)
    batch = SimpleNamespace(
        rows=[
            SimpleNamespace(
                start_at=_utc(duty, time(19, 0)),
                end_at=_utc(duty, time(22, 0)),
                matched_pharmacy=SimpleNamespace(name="نهاري من المسودة"),
                raw_data={"period": "day"},
            ),
            SimpleNamespace(
                start_at=_utc(duty, time(17, 0)),
                end_at=_utc(duty, time(20, 0)),
                matched_pharmacy=SimpleNamespace(name="مسائي من المسودة"),
                raw_data={"period": "evening"},
            ),
        ]
    )

    views = smart_ui.draft_shift_views(batch)
    assert [(view.pharmacy.name, view.period) for view in views] == [
        ("نهاري من المسودة", "day"),
        ("مسائي من المسودة", "evening"),
    ]
