from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from app.services import smart_schedule as smart
from app.services.shift_schedule_tools import ShiftTimes


FAIR_ALGORITHM = "deterministic-fair-v2"
TOTAL_FAIRNESS_BAND = 1


@dataclass(frozen=True, slots=True)
class _FairContext:
    offsets: dict[int, tuple[int, int, int]]
    order: tuple[int, ...]
    positions: dict[int, int]


def _is_new(ledger: smart.PharmacyLedger) -> bool:
    return (
        ledger.total == 0
        and ledger.day == 0
        and ledger.evening == 0
        and ledger.last_date is None
    )


def _build_context(base_ledgers: list[smart.PharmacyLedger]) -> _FairContext:
    """Build deterministic scoring context.

    A newly-added pharmacy gets virtual starting counts equal to the lowest
    established counts. This makes it join the rotation gradually instead of
    receiving a large catch-up block only because it has no historical shifts.
    """
    established = [ledger for ledger in base_ledgers if not _is_new(ledger)]
    if established:
        baseline = (
            min(ledger.total for ledger in established),
            min(ledger.day for ledger in established),
            min(ledger.evening for ledger in established),
        )
    else:
        baseline = (0, 0, 0)

    offsets: dict[int, tuple[int, int, int]] = {}
    for ledger in base_ledgers:
        offsets[ledger.pharmacy_id] = baseline if _is_new(ledger) else (0, 0, 0)

    order = tuple(sorted(ledger.pharmacy_id for ledger in base_ledgers))
    positions = {pharmacy_id: index for index, pharmacy_id in enumerate(order)}
    return _FairContext(offsets=offsets, order=order, positions=positions)


def _effective_total(ledger: smart.PharmacyLedger, context: _FairContext) -> int:
    return ledger.total + context.offsets.get(ledger.pharmacy_id, (0, 0, 0))[0]


def _effective_period(ledger: smart.PharmacyLedger, period: str, context: _FairContext) -> int:
    offset = context.offsets.get(ledger.pharmacy_id, (0, 0, 0))
    if period == smart.DAY:
        return ledger.day + offset[1]
    return ledger.evening + offset[2]


def _rest_days(ledger: smart.PharmacyLedger, duty_date: date) -> int:
    # A brand-new pharmacy must not receive an artificial "infinite rest"
    # advantage. Its virtual counts already put it fairly into the rotation.
    if ledger.last_date is None:
        return 0
    return max(0, (duty_date - ledger.last_date).days)


def _rotation_rank(
    ledger: smart.PharmacyLedger,
    *,
    duty_date: date,
    period: str,
    context: _FairContext,
) -> int:
    """Deterministic round-robin tie breaker.

    The preferred starting pharmacy changes by date and by day/evening period,
    so a perfect tie does not permanently favor the same pharmacy or shift type.
    """
    size = len(context.order)
    if size <= 1:
        return 0
    period_offset = 0 if period == smart.DAY else 1
    start = (duty_date.toordinal() * 2 + period_offset) % size
    position = context.positions.get(ledger.pharmacy_id, 0)
    return (position - start) % size


def _pick_candidate(
    ledgers: list[smart.PharmacyLedger],
    *,
    duty_date: date,
    period: str,
    excluded_ids: set[int],
    previous_day_ids: set[int],
    context: _FairContext,
) -> smart.PharmacyLedger:
    candidates = [ledger for ledger in ledgers if ledger.pharmacy_id not in excluded_ids]

    # Friday is a hard annual-cycle rule: 0/2 first, then 1/2, never 3/2.
    if duty_date.weekday() == 4:
        candidates = [ledger for ledger in candidates if ledger.friday_count < smart.FRIDAY_LIMIT]
        if not candidates:
            raise ValueError("لا توجد صيدلية متاحة للجمعة بدون تجاوز حد جمعتين في الدورة.")
        minimum_fridays = min(ledger.friday_count for ledger in candidates)
        candidates = [ledger for ledger in candidates if ledger.friday_count == minimum_fridays]

    # Avoid consecutive days whenever another valid pharmacy exists.
    non_consecutive = [
        ledger for ledger in candidates if ledger.pharmacy_id not in previous_day_ids
    ]
    if non_consecutive:
        candidates = non_consecutive
    if not candidates:
        raise ValueError("لا توجد صيدلية متاحة لهذه المناوبة وفق القيود الحالية.")

    # Keep the overall total close, but allow a one-shift band so the scheduler
    # can correct day/evening imbalance instead of trapping a pharmacy in the
    # same type of duty merely to preserve an exact total tie.
    minimum_total = min(_effective_total(ledger, context) for ledger in candidates)
    total_band = [
        ledger
        for ledger in candidates
        if _effective_total(ledger, context) <= minimum_total + TOTAL_FAIRNESS_BAND
    ]

    minimum_period = min(_effective_period(ledger, period, context) for ledger in total_band)
    period_priority = [
        ledger
        for ledger in total_band
        if _effective_period(ledger, period, context) == minimum_period
    ]

    return min(
        period_priority,
        key=lambda ledger: (
            _effective_total(ledger, context),
            -_rest_days(ledger, duty_date),
            _rotation_rank(
                ledger,
                duty_date=duty_date,
                period=period,
                context=context,
            ),
            ledger.pharmacy_id,
        ),
    )


def _generate_once(
    base_ledgers: list[smart.PharmacyLedger],
    *,
    start_date: date,
    end_date: date,
    times: ShiftTimes,
    timezone: ZoneInfo,
    fixed: Mapping[tuple[date, str], int],
) -> list[smart.SmartAssignment]:
    ledgers = [item.clone() for item in base_ledgers]
    by_id = {item.pharmacy_id: item for item in ledgers}
    context = _build_context(base_ledgers)
    assignments: list[smart.SmartAssignment] = []
    assignments_by_date: dict[date, list[smart.SmartAssignment]] = {}

    current = start_date
    while current <= end_date:
        assignments_by_date.setdefault(current, [])
        previous_day_ids = {
            item.pharmacy_id
            for item in assignments_by_date.get(current - timedelta(days=1), [])
        }
        fixed_ids_today = {
            pharmacy_id
            for (fixed_date, _period), pharmacy_id in fixed.items()
            if fixed_date == current
        }

        for period in smart.PERIODS:
            fixed_id = fixed.get((current, period))
            if fixed_id is not None:
                ledger = by_id.get(fixed_id)
                if ledger is None:
                    raise ValueError("إحدى المناوبات المثبتة مرتبطة بصيدلية غير فعالة.")
                chosen = ledger
                locked = True
            else:
                used_today = {item.pharmacy_id for item in assignments_by_date[current]}
                excluded = used_today | fixed_ids_today
                chosen = _pick_candidate(
                    ledgers,
                    duty_date=current,
                    period=period,
                    excluded_ids=excluded,
                    previous_day_ids=previous_day_ids,
                    context=context,
                )
                locked = False

            start_at, end_at = smart._assignment_times(current, period, times, timezone)
            assignment = smart.SmartAssignment(
                duty_date=current,
                period=period,
                pharmacy_id=chosen.pharmacy_id,
                pharmacy_name=chosen.name,
                start_at=start_at,
                end_at=end_at,
                locked=locked,
            )
            assignments.append(assignment)
            assignments_by_date[current].append(assignment)
            smart._apply_assignment(chosen, assignment)

        current += timedelta(days=1)

    return assignments


def generate_best_schedule(
    base_ledgers: list[smart.PharmacyLedger],
    *,
    start_date: date,
    end_date: date,
    times: ShiftTimes,
    timezone: ZoneInfo,
    fixed: Mapping[tuple[date, str], int] | None = None,
    attempts: int = 40,
    seed: int | None = None,
) -> tuple[list[smart.SmartAssignment], smart.ScheduleAnalysis]:
    """Generate one deterministic, fairness-driven schedule.

    ``attempts`` and ``seed`` remain in the signature for compatibility with
    older callers, but are intentionally ignored. Same inputs always produce
    the same schedule.
    """
    del attempts, seed

    if end_date < start_date:
        raise ValueError("تاريخ نهاية الجدول يجب أن يكون بعد البداية.")
    if (end_date - start_date).days > 92:
        raise ValueError("الحد الأقصى لإنشاء جدول دفعة واحدة هو 93 يوماً.")
    if len(base_ledgers) < 2:
        raise ValueError("يلزم وجود صيدليتين فعالتين على الأقل لإنشاء جدول.")

    fixed = fixed or {}
    friday_days = 0
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() == 4:
            friday_days += 1
        cursor += timedelta(days=1)

    remaining_friday_capacity = sum(
        max(0, smart.FRIDAY_LIMIT - item.friday_count) for item in base_ledgers
    )
    required_slots = friday_days * 2
    if remaining_friday_capacity < required_slots:
        raise ValueError(
            "لا يمكن توزيع كل جمعات هذه الفترة ضمن حد جمعتين لكل صيدلية. "
            f"المطلوب {required_slots} خانات جمعة والمتاح {remaining_friday_capacity}."
        )

    assignments = _generate_once(
        base_ledgers,
        start_date=start_date,
        end_date=end_date,
        times=times,
        timezone=timezone,
        fixed=fixed,
    )
    analysis = smart.analyze_assignments(assignments, base_ledgers)
    return assignments, analysis


# This patch is loaded after the Friday-cycle integration patch. Its only job is
# to replace random schedule selection with deterministic fair selection.
smart.SMART_ALGORITHM = FAIR_ALGORITHM
smart.generate_best_schedule = generate_best_schedule
