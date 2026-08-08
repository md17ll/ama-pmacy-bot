from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.shift_schedule_tools import ShiftTimes
from app.services.smart_schedule import (
    DAY,
    EVENING,
    PharmacyLedger,
    SmartAssignment,
    analyze_assignments,
    default_period,
    generate_best_schedule,
)


TZ = ZoneInfo("Asia/Damascus")
TIMES = ShiftTimes()


def _ledger(pharmacy_id: int, *, total: int = 0, day: int = 0, evening: int = 0, fridays: int = 0) -> PharmacyLedger:
    friday_dates = {date(2026, 1, 2 + 7 * index) for index in range(fridays)}
    return PharmacyLedger(
        pharmacy_id=pharmacy_id,
        name=f"صيدلية {pharmacy_id}",
        total=total,
        day=day,
        evening=evening,
        friday_dates=friday_dates,
    )


def test_default_period_starts_after_latest_day_and_spans_one_cycle() -> None:
    latest = datetime(2026, 8, 17, 20, 30, tzinfo=timezone.utc)
    start_date, end_date = default_period(latest, TZ)
    assert start_date == date(2026, 8, 18)
    assert end_date == date(2026, 9, 17)


def test_generated_schedule_never_uses_same_pharmacy_twice_same_day() -> None:
    ledgers = [_ledger(index) for index in range(1, 9)]
    assignments, analysis = generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 24),
        times=TIMES,
        timezone=TZ,
        seed=11,
    )
    assert len(assignments) == 14
    for duty_date in {item.duty_date for item in assignments}:
        ids = [item.pharmacy_id for item in assignments if item.duty_date == duty_date]
        assert len(ids) == 2
        assert len(set(ids)) == 2
    assert analysis.same_day_conflicts == 0


def test_generator_avoids_consecutive_days_when_enough_pharmacies_exist() -> None:
    ledgers = [_ledger(index) for index in range(1, 9)]
    _, analysis = generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 27),
        times=TIMES,
        timezone=TZ,
        seed=21,
    )
    assert analysis.consecutive_assignments == 0


def test_friday_prefers_zero_of_two_before_one_and_excludes_two_of_two() -> None:
    ledgers = [
        _ledger(1, fridays=0),
        _ledger(2, fridays=0),
        _ledger(3, fridays=1),
        _ledger(4, fridays=2),
    ]
    assignments, analysis = generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 21),
        end_date=date(2026, 8, 21),
        times=TIMES,
        timezone=TZ,
        seed=7,
    )
    assert {item.pharmacy_id for item in assignments} == {1, 2}
    assert analysis.friday_over_limit == 0
    assert analysis.friday_priority_violations == 0


def test_analysis_flags_same_day_conflict_and_friday_over_limit() -> None:
    base = [_ledger(1, fridays=2), _ledger(2)]
    friday = date(2026, 8, 21)
    assignments = [
        SmartAssignment(
            duty_date=friday,
            period=DAY,
            pharmacy_id=1,
            pharmacy_name="صيدلية 1",
            start_at=datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc),
        ),
        SmartAssignment(
            duty_date=friday,
            period=EVENING,
            pharmacy_id=1,
            pharmacy_name="صيدلية 1",
            start_at=datetime(2026, 8, 21, 17, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 8, 21, 20, 30, tzinfo=timezone.utc),
        ),
    ]
    analysis = analyze_assignments(assignments, base)
    assert analysis.same_day_conflicts == 1
    assert analysis.friday_over_limit == 1
    assert analysis.hard_errors == 2


def test_generator_refuses_period_when_friday_capacity_is_insufficient() -> None:
    ledgers = [_ledger(1, fridays=2), _ledger(2, fridays=2), _ledger(3, fridays=1)]
    with pytest.raises(ValueError, match="خانات جمعة"):
        generate_best_schedule(
            ledgers,
            start_date=date(2026, 8, 21),
            end_date=date(2026, 8, 21),
            times=TIMES,
            timezone=TZ,
            seed=1,
        )
