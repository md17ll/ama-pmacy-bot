from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.services.friday_history import (
    FRIDAY_PAIRS_2026,
    REFERENCE_THROUGH,
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
    assert summary["sources"] == 4
    assert FRIDAY_PAIRS_2026[0] == (date(2026, 1, 2), "عامودا", "سيدو")
    assert FRIDAY_PAIRS_2026[-1] == (date(2026, 8, 14), "رشاد", "زنار")


def test_reference_matches_spacing_alias_and_keeps_real_dates() -> None:
    pharmacies = [_pharmacy(1, "محمد حسو", "محمدحسو")]
    result = friday_history_for_pharmacies(
        pharmacies,
        year=2026,
        before_date=date(2026, 8, 18),
    )
    assert result[1].floor == 2
    assert date(2026, 3, 13) in result[1].dates
    assert date(2026, 8, 7) in result[1].dates


def test_reference_does_not_leak_future_rows_into_backtest() -> None:
    pharmacies = [_pharmacy(1, "رشاد")]
    result = friday_history_for_pharmacies(
        pharmacies,
        year=2026,
        before_date=date(2026, 8, 8),
    )
    assert result[1].floor == 0
    assert date(2026, 1, 9) in result[1].dates
    assert date(2026, 8, 14) not in result[1].dates


def test_reference_resets_for_new_year() -> None:
    assert friday_history_for_pharmacies(
        [_pharmacy(1, "شمس")],
        year=2027,
        before_date=date(2027, 1, 2),
    ) == {}


def test_scheduler_uses_2_of_2_quota_without_double_counting_database_date() -> None:
    pharmacy = _pharmacy(1, "شمس")
    ledger = PharmacyLedger(
        pharmacy_id=1,
        name="شمس",
        friday_dates={date(2026, 7, 17)},
    )
    merged = _merge_reference_into_ledgers(
        [pharmacy],
        [ledger],
        year=2026,
        before_date=date(2026, 8, 18),
    )[0]
    # The count page records Shams as 2/2.  The dated 17/7 record is already
    # present in the database here, so it must not be double-counted.
    assert merged.friday_count == 2
    assert date(2026, 7, 17) in merged.friday_dates
    assert REFERENCE_THROUGH == date(2026, 8, 14)
