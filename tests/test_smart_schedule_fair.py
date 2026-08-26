from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import app.handlers  # noqa: F401 - activates smart scheduler patches
from app.services import smart_schedule as smart
from app.services import smart_schedule_fair_patch as _smart_schedule_fair_patch  # noqa: F401
from app.services.shift_schedule_tools import ShiftTimes


TZ = ZoneInfo("Asia/Damascus")
TIMES = ShiftTimes()


def _ledger(
    pharmacy_id: int,
    *,
    total: int = 0,
    day: int = 0,
    evening: int = 0,
    fridays: int = 0,
    last_date: date | None = None,
) -> smart.PharmacyLedger:
    friday_dates = {date(2026, 8, 7) + timedelta(days=7 * index) for index in range(fridays)}
    return smart.PharmacyLedger(
        pharmacy_id=pharmacy_id,
        name=f"صيدلية {pharmacy_id}",
        total=total,
        day=day,
        evening=evening,
        friday_dates=friday_dates,
        last_date=last_date,
    )


def _signature(assignments: list[smart.SmartAssignment]) -> list[tuple[date, str, int]]:
    return [(item.duty_date, item.period, item.pharmacy_id) for item in assignments]


def test_same_inputs_always_produce_same_schedule_even_with_different_seeds() -> None:
    ledgers = [
        _ledger(index, total=8, day=4, evening=4, last_date=date(2026, 8, 10))
        for index in range(1, 9)
    ]
    first, _ = smart.generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 31),
        times=TIMES,
        timezone=TZ,
        seed=1,
        attempts=1,
    )
    second, _ = smart.generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 31),
        times=TIMES,
        timezone=TZ,
        seed=999999,
        attempts=100,
    )
    assert _signature(first) == _signature(second)
    assert smart.SMART_ALGORITHM == "deterministic-fair-v2"


def test_day_and_evening_history_are_balanced_in_the_needed_direction() -> None:
    ledgers = [
        _ledger(1, total=10, day=7, evening=3, last_date=date(2026, 8, 15)),
        _ledger(2, total=10, day=4, evening=6, last_date=date(2026, 8, 15)),
        _ledger(3, total=10, day=5, evening=5, last_date=date(2026, 8, 14)),
        _ledger(4, total=10, day=5, evening=5, last_date=date(2026, 8, 14)),
    ]
    assignments, _ = smart.generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 18),
        times=TIMES,
        timezone=TZ,
    )
    by_period = {item.period: item.pharmacy_id for item in assignments}
    assert by_period[smart.DAY] == 2
    assert by_period[smart.EVENING] == 1


def test_scheduler_keeps_each_period_balanced_over_a_longer_run() -> None:
    ledgers = [
        _ledger(index, total=8, day=4, evening=4, last_date=date(2026, 8, 10))
        for index in range(1, 9)
    ]
    assignments, analysis = smart.generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 18),
        end_date=date(2026, 9, 17),
        times=TIMES,
        timezone=TZ,
    )
    day_counts = Counter(item.pharmacy_id for item in assignments if item.period == smart.DAY)
    evening_counts = Counter(item.pharmacy_id for item in assignments if item.period == smart.EVENING)
    assert max(day_counts.values()) - min(day_counts.values()) <= 1
    assert max(evening_counts.values()) - min(evening_counts.values()) <= 1
    assert analysis.same_day_conflicts == 0


def test_new_pharmacy_joins_gradually_instead_of_receiving_catch_up_block() -> None:
    established = [
        _ledger(index, total=20, day=10, evening=10, last_date=date(2026, 8, 15))
        for index in range(1, 5)
    ]
    new_pharmacy = _ledger(5)
    assignments, _ = smart.generate_best_schedule(
        established + [new_pharmacy],
        start_date=date(2026, 8, 18),
        end_date=date(2026, 8, 31),
        times=TIMES,
        timezone=TZ,
    )
    counts = Counter(item.pharmacy_id for item in assignments)
    assert counts[5] <= max(counts[index] for index in range(1, 5)) + 1
    assert counts[5] >= min(counts[index] for index in range(1, 5)) - 1


def test_friday_rule_remains_strict_under_deterministic_scheduler() -> None:
    ledgers = [
        _ledger(1, total=5, day=3, evening=2, fridays=0),
        _ledger(2, total=5, day=2, evening=3, fridays=0),
        _ledger(3, total=1, day=1, evening=0, fridays=1),
        _ledger(4, total=0, day=0, evening=0, fridays=2),
    ]
    assignments, analysis = smart.generate_best_schedule(
        ledgers,
        start_date=date(2026, 8, 21),
        end_date=date(2026, 8, 21),
        times=TIMES,
        timezone=TZ,
    )
    assert {item.pharmacy_id for item in assignments} == {1, 2}
    assert analysis.friday_over_limit == 0
    assert analysis.friday_priority_violations == 0
