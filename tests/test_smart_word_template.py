from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from docx import Document

from app.services.smart_word_template import (
    TEMPLATE_SHA256,
    build_smart_template_schedule,
)
from app.services.word_export import WordExportError
from app.services.word_schedule import parse_amuda_word_schedule


TZ = ZoneInfo("Asia/Damascus")


def _utc(duty_date: date, value: time) -> datetime:
    return datetime.combine(duty_date, value, tzinfo=TZ).astimezone(timezone.utc)


def _shifts(first: date, days: int, *, include_evening: bool = True):
    result = []
    for offset in range(days):
        duty_date = first + timedelta(days=offset)
        result.append(
            SimpleNamespace(
                start_at=_utc(duty_date, time(13, 30)),
                end_at=_utc(duty_date, time(17, 0)),
                active=True,
                pharmacy=SimpleNamespace(name=f"نهاري {offset + 1}"),
            )
        )
        if include_evening or offset:
            result.append(
                SimpleNamespace(
                    start_at=_utc(duty_date, time(20, 30)),
                    end_at=_utc(duty_date, time(23, 30)),
                    active=True,
                    pharmacy=SimpleNamespace(name=f"مسائي {offset + 1}"),
                )
            )
    return result


def test_smart_word_uses_exact_official_template_and_maps_day_evening_names() -> None:
    first = date(2026, 8, 15)
    last = first + timedelta(days=30)
    data = build_smart_template_schedule(
        _shifts(first, 31),
        TZ,
        period_start=first,
        period_end=last,
    )
    document = Document(BytesIO(data))

    assert len(TEMPLATE_SHA256) == 64
    assert len(document.tables) == 1
    table = document.tables[0]
    assert len(table.rows) == 20
    assert len(table.columns) == 6
    assert table.rows[0].cells[0].text == "اليوم والتاريخ"
    assert table.rows[0].cells[1].text == "النهارية: \n١.٣٠-٥"
    assert table.rows[0].cells[2].text == "المساء:\n٨.٣٠-١١.٣٠"

    # The template has 19 date slots on the first side, then 19 on the second.
    assert table.rows[1].cells[0].text == "السبت: 15/8/2026"
    assert table.rows[1].cells[1].text == "نهاري 1"
    assert table.rows[1].cells[2].text == "مسائي 1"
    assert table.rows[1].cells[3].text == "الخميس: 3/9/2026"
    assert table.rows[1].cells[4].text == "نهاري 20"
    assert table.rows[1].cells[5].text == "مسائي 20"

    # Unused template slots remain blank rather than changing the table shape.
    assert table.rows[19].cells[3].text == ""
    assert table.rows[19].cells[4].text == ""
    assert table.rows[19].cells[5].text == ""

    assert any("شهر//8// آب لعام 2026" in paragraph.text for paragraph in document.paragraphs)
    assert any(paragraph.text == "الدوام المسائي:5:00 – 8:30" for paragraph in document.paragraphs)

    parsed, warnings = parse_amuda_word_schedule(data)
    assert not warnings
    assert len(parsed) == 62
    assert parsed[0].pharmacy_name == "نهاري 1"
    assert parsed[1].pharmacy_name == "مسائي 1"
    assert parsed[-2].pharmacy_name == "نهاري 31"
    assert parsed[-1].pharmacy_name == "مسائي 31"


def test_smart_word_repeats_same_template_for_more_than_38_days() -> None:
    first = date(2026, 8, 15)
    last = first + timedelta(days=44)
    data = build_smart_template_schedule(
        _shifts(first, 45),
        TZ,
        period_start=first,
        period_end=last,
    )
    document = Document(BytesIO(data))

    assert len(document.tables) == 2
    assert all(len(table.rows) == 20 and len(table.columns) == 6 for table in document.tables)
    second = document.tables[1]
    assert second.rows[1].cells[1].text == "نهاري 39"
    assert second.rows[1].cells[2].text == "مسائي 39"
    assert any("شهر//9// أيلول لعام 2026" in paragraph.text for paragraph in document.paragraphs)


def test_smart_word_refuses_incomplete_day_evening_pair() -> None:
    first = date(2026, 8, 15)
    with pytest.raises(WordExportError, match="صيدلية نهارية وصيدلية مسائية"):
        build_smart_template_schedule(
            _shifts(first, 2, include_evening=False),
            TZ,
            period_start=first,
            period_end=first + timedelta(days=1),
        )
