from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.friday_history import (
    FRIDAY_PAIRS_2026,
    REFERENCE_THROUGH,
    friday_cycle_for,
    friday_history_for_pharmacies,
    reference_summary,
)
from app.services.smart_schedule import PharmacyLedger
from app.services.smart_schedule_history_patch import _merge_reference_into_ledgers


def _pharmacy(pharmacy_id: int, name: str, *aliases: str):
    return SimpleNamespace(
        id=pharmacy_id,
        name=name,
        aliases=[SimpleNamespace(alias=value) for value in aliases],
    )


def test_reference_contains_all_dated_fridays_through_august_14() -> None:
    summary = reference_summary()
    assert summary["through"] == "2026-08-14"
    assert summary["dated_fridays"] == 33
    assert summary["dated_assignments"] == 66
    assert summary["cycle_start_month"] == 8
    assert summary["sources"] == 4
    assert FRIDAY_PAIRS_2026[0] == (date(2026, 1, 2), "عامودا", "سيدو")
    assert FRIDAY_PAIRS_2026[-1] == (date(2026, 8, 14), "رشاد", "زنار")


def test_friday_cycle_resets_on_august_first_not_january_first() -> None:
    assert friday_cycle_for(date(2026, 7, 31)).start == date(2025, 8, 1)
    assert friday_cycle_for(date(2026, 7, 31)).end == date(2026, 7, 31)
    assert friday_cycle_for(date(2026, 8, 1)).start == date(2026, 8, 1)
    assert friday_cycle_for(date(2027, 1, 1)).start == date(2026, 8, 1)
    assert friday_cycle_for(date(2027, 7, 31)).end == date(2027, 7, 31)
    assert friday_cycle_for(date(2027, 8, 1)).start == date(2027, 8, 1)


def test_new_cycle_uses_only_august_photo_dates() -> None:
    pharmacies = [_pharmacy(1, "محمد حسو", "محمدحسو")]
    result = friday_history_for_pharmacies(
        pharmacies,
        year=2026,
        before_date=date(2026, 8, 18),
    )
    assert result[1].floor == 0
    assert result[1].dates == frozenset({date(2026, 8, 7)})
    assert date(2026, 3, 13) not in result[1].dates


def test_reference_does_not_leak_future_photo_row() -> None:
    pharmacies = [_pharmacy(1, "رشاد")]
    result = friday_history_for_pharmacies(
        pharmacies,
        year=2026,
        before_date=date(2026, 8, 8),
    )
    assert result == {}


def test_january_2027_is_still_same_august_cycle() -> None:
    result = friday_history_for_pharmacies(
        [_pharmacy(1, "رشاد")],
        year=2027,
        before_date=date(2027, 1, 2),
    )
    assert result[1].dates == frozenset({date(2026, 8, 14)})


def test_scheduler_drops_previous_cycle_database_friday() -> None:
    pharmacy = _pharmacy(1, "عصام")
    ledger = PharmacyLedger(
        pharmacy_id=1,
        name="عصام",
        friday_dates={date(2026, 2, 20)},
    )
    merged = _merge_reference_into_ledgers(
        [pharmacy],
        [ledger],
        year=2026,
        before_date=date(2026, 8, 18),
    )[0]
    assert merged.friday_count == 1
    assert merged.friday_dates == {date(2026, 8, 7)}
    assert REFERENCE_THROUGH == date(2026, 8, 14)
