from __future__ import annotations

import random
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import ImportBatch, Pharmacy, Shift
from app.services.shift_schedule_tools import ShiftTimes
from app.utils import as_local, combine_shift, utcnow


DAY = "day"
EVENING = "evening"
PERIODS = (DAY, EVENING)
FRIDAY_LIMIT = 2
SMART_SOURCE_TYPE = "smart"
SMART_ALGORITHM = "balanced-v1"


@dataclass(slots=True)
class PharmacyLedger:
    pharmacy_id: int
    name: str
    total: int = 0
    day: int = 0
    evening: int = 0
    friday_dates: set[date] = field(default_factory=set)
    last_date: date | None = None

    @property
    def friday_count(self) -> int:
        return len(self.friday_dates)

    def period_count(self, period: str) -> int:
        return self.day if period == DAY else self.evening

    def clone(self) -> "PharmacyLedger":
        return PharmacyLedger(
            pharmacy_id=self.pharmacy_id,
            name=self.name,
            total=self.total,
            day=self.day,
            evening=self.evening,
            friday_dates=set(self.friday_dates),
            last_date=self.last_date,
        )


@dataclass(frozen=True, slots=True)
class SmartAssignment:
    duty_date: date
    period: str
    pharmacy_id: int
    pharmacy_name: str
    start_at: datetime
    end_at: datetime
    locked: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleAnalysis:
    total_assignments: int
    total_spread: int
    day_spread: int
    evening_spread: int
    same_day_conflicts: int
    consecutive_assignments: int
    friday_over_limit: int
    friday_priority_violations: int
    friday_assignments: int
    min_total: int
    max_total: int
    rating: str
    penalty: int

    @property
    def hard_errors(self) -> int:
        return self.same_day_conflicts + self.friday_over_limit

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_assignments": self.total_assignments,
            "total_spread": self.total_spread,
            "day_spread": self.day_spread,
            "evening_spread": self.evening_spread,
            "same_day_conflicts": self.same_day_conflicts,
            "consecutive_assignments": self.consecutive_assignments,
            "friday_over_limit": self.friday_over_limit,
            "friday_priority_violations": self.friday_priority_violations,
            "friday_assignments": self.friday_assignments,
            "min_total": self.min_total,
            "max_total": self.max_total,
            "rating": self.rating,
            "penalty": self.penalty,
            "hard_errors": self.hard_errors,
        }


@dataclass(frozen=True, slots=True)
class DraftShift:
    start_at: datetime
    end_at: datetime
    pharmacy: Any
    active: bool = True


def next_month_period(start: date) -> tuple[date, date]:
    if start.month == 12:
        next_month = date(start.year + 1, 1, min(start.day, 31))
    else:
        target_month = start.month + 1
        target_day = min(start.day, monthrange(start.year, target_month)[1])
        next_month = date(start.year, target_month, target_day)
    return start, next_month - timedelta(days=1)


def default_period(latest_end: datetime | None, timezone: ZoneInfo, now: datetime | None = None) -> tuple[date, date]:
    if latest_end is None:
        local_now = as_local(now or utcnow(), timezone)
        start = local_now.date()
    else:
        start = as_local(latest_end, timezone).date() + timedelta(days=1)
    return next_month_period(start)


def _period_for_start(value: datetime, timezone: ZoneInfo) -> str:
    local = as_local(value, timezone)
    return DAY if local.time().replace(tzinfo=None) < time(18, 0) else EVENING


def _build_ledgers(
    pharmacies: Iterable[Pharmacy],
    shifts: Iterable[Shift],
    *,
    year: int,
    timezone: ZoneInfo,
    before_date: date | None = None,
) -> list[PharmacyLedger]:
    ledgers = {
        pharmacy.id: PharmacyLedger(pharmacy_id=pharmacy.id, name=pharmacy.name)
        for pharmacy in pharmacies
    }
    for shift in sorted(shifts, key=lambda item: item.start_at):
        ledger = ledgers.get(shift.pharmacy_id)
        if ledger is None:
            continue
        local = as_local(shift.start_at, timezone)
        duty_date = local.date()
        if before_date is not None and duty_date >= before_date:
            continue
        if ledger.last_date is None or duty_date > ledger.last_date:
            ledger.last_date = duty_date
        if duty_date.year != year:
            continue
        ledger.total += 1
        period = DAY if local.time().replace(tzinfo=None) < time(18, 0) else EVENING
        if period == DAY:
            ledger.day += 1
        else:
            ledger.evening += 1
        if duty_date.weekday() == 4:
            ledger.friday_dates.add(duty_date)
    return list(ledgers.values())


def _apply_assignment(ledger: PharmacyLedger, assignment: SmartAssignment) -> None:
    ledger.total += 1
    if assignment.period == DAY:
        ledger.day += 1
    else:
        ledger.evening += 1
    if assignment.duty_date.weekday() == 4:
        ledger.friday_dates.add(assignment.duty_date)
    ledger.last_date = assignment.duty_date


def _candidate_score(
    ledger: PharmacyLedger,
    period: str,
    duty_date: date,
    *,
    previous_day_ids: set[int],
    rng: random.Random,
) -> tuple[int, int, int, int, float]:
    consecutive_penalty = 1 if ledger.pharmacy_id in previous_day_ids else 0
    if ledger.last_date is None:
        rest_days = 9999
    else:
        rest_days = max(0, (duty_date - ledger.last_date).days)
    return (
        consecutive_penalty,
        ledger.total,
        ledger.period_count(period),
        -rest_days,
        rng.random(),
    )


def _pick_candidate(
    ledgers: list[PharmacyLedger],
    *,
    duty_date: date,
    period: str,
    excluded_ids: set[int],
    previous_day_ids: set[int],
    rng: random.Random,
) -> PharmacyLedger:
    candidates = [ledger for ledger in ledgers if ledger.pharmacy_id not in excluded_ids]
    if duty_date.weekday() == 4:
        candidates = [ledger for ledger in candidates if ledger.friday_count < FRIDAY_LIMIT]
        if not candidates:
            raise ValueError("لا توجد صيدلية متاحة للجمعة بدون تجاوز حد جمعتين في السنة.")
        minimum_fridays = min(ledger.friday_count for ledger in candidates)
        priority = [ledger for ledger in candidates if ledger.friday_count == minimum_fridays]
        if priority:
            candidates = priority

    non_consecutive = [ledger for ledger in candidates if ledger.pharmacy_id not in previous_day_ids]
    if non_consecutive:
        candidates = non_consecutive
    if not candidates:
        raise ValueError("لا توجد صيدلية متاحة لهذه المناوبة وفق القيود الحالية.")
    return min(
        candidates,
        key=lambda ledger: _candidate_score(
            ledger,
            period,
            duty_date,
            previous_day_ids=previous_day_ids,
            rng=rng,
        ),
    )


def _assignment_times(duty_date: date, period: str, times: ShiftTimes, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    if period == DAY:
        return combine_shift(duty_date, times.day_start, times.day_end, timezone)
    return combine_shift(duty_date, times.evening_start, times.evening_end, timezone)


def _generate_once(
    base_ledgers: list[PharmacyLedger],
    *,
    start_date: date,
    end_date: date,
    times: ShiftTimes,
    timezone: ZoneInfo,
    fixed: Mapping[tuple[date, str], int],
    rng: random.Random,
) -> list[SmartAssignment]:
    ledgers = [item.clone() for item in base_ledgers]
    by_id = {item.pharmacy_id: item for item in ledgers}
    assignments: list[SmartAssignment] = []
    assignments_by_date: dict[date, list[SmartAssignment]] = defaultdict(list)

    current = start_date
    while current <= end_date:
        previous_day_ids = {
            item.pharmacy_id for item in assignments_by_date.get(current - timedelta(days=1), [])
        }
        fixed_ids_today = {
            pharmacy_id
            for (fixed_date, _period), pharmacy_id in fixed.items()
            if fixed_date == current
        }
        for period in PERIODS:
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
                    rng=rng,
                )
                locked = False
            start_at, end_at = _assignment_times(current, period, times, timezone)
            assignment = SmartAssignment(
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
            _apply_assignment(chosen, assignment)
        current += timedelta(days=1)
    return assignments


def analyze_assignments(assignments: Iterable[SmartAssignment], base_ledgers: Iterable[PharmacyLedger]) -> ScheduleAnalysis:
    assignments = sorted(assignments, key=lambda item: (item.duty_date, item.period))
    ledgers = {item.pharmacy_id: item.clone() for item in base_ledgers}
    same_day_conflicts = 0
    consecutive = 0
    friday_priority_violations = 0
    by_date: dict[date, list[SmartAssignment]] = defaultdict(list)

    for assignment in assignments:
        by_date[assignment.duty_date].append(assignment)

    for duty_date, items in by_date.items():
        ids = [item.pharmacy_id for item in items]
        same_day_conflicts += len(ids) - len(set(ids))

    previous_ids: set[int] = set()
    previous_date: date | None = None
    for duty_date in sorted(by_date):
        current_ids = {item.pharmacy_id for item in by_date[duty_date]}
        if previous_date is not None and duty_date == previous_date + timedelta(days=1):
            consecutive += len(current_ids & previous_ids)
        previous_ids = current_ids
        previous_date = duty_date

    for assignment in assignments:
        ledger = ledgers.get(assignment.pharmacy_id)
        if ledger is None:
            continue
        if assignment.duty_date.weekday() == 4:
            eligible = [item for item in ledgers.values() if item.friday_count < FRIDAY_LIMIT]
            if eligible:
                minimum = min(item.friday_count for item in eligible)
                if ledger.friday_count > minimum:
                    friday_priority_violations += 1
        _apply_assignment(ledger, assignment)

    totals = [item.total for item in ledgers.values()]
    day_counts = [item.day for item in ledgers.values()]
    evening_counts = [item.evening for item in ledgers.values()]
    friday_over = sum(max(0, item.friday_count - FRIDAY_LIMIT) for item in ledgers.values())
    total_spread = (max(totals) - min(totals)) if totals else 0
    day_spread = (max(day_counts) - min(day_counts)) if day_counts else 0
    evening_spread = (max(evening_counts) - min(evening_counts)) if evening_counts else 0
    penalty = (
        same_day_conflicts * 1000
        + friday_over * 1000
        + friday_priority_violations * 80
        + consecutive * 40
        + total_spread * 12
        + day_spread * 3
        + evening_spread * 3
    )
    if same_day_conflicts or friday_over:
        rating = "يحتاج تعديل"
    elif consecutive == 0 and total_spread <= 1 and friday_priority_violations == 0:
        rating = "ممتاز"
    elif consecutive <= 2 and total_spread <= 2 and friday_priority_violations == 0:
        rating = "جيد جداً"
    else:
        rating = "جيد مع تنبيهات"
    return ScheduleAnalysis(
        total_assignments=len(assignments),
        total_spread=total_spread,
        day_spread=day_spread,
        evening_spread=evening_spread,
        same_day_conflicts=same_day_conflicts,
        consecutive_assignments=consecutive,
        friday_over_limit=friday_over,
        friday_priority_violations=friday_priority_violations,
        friday_assignments=sum(1 for item in assignments if item.duty_date.weekday() == 4),
        min_total=min(totals) if totals else 0,
        max_total=max(totals) if totals else 0,
        rating=rating,
        penalty=penalty,
    )


def generate_best_schedule(
    base_ledgers: list[PharmacyLedger],
    *,
    start_date: date,
    end_date: date,
    times: ShiftTimes,
    timezone: ZoneInfo,
    fixed: Mapping[tuple[date, str], int] | None = None,
    attempts: int = 40,
    seed: int | None = None,
) -> tuple[list[SmartAssignment], ScheduleAnalysis]:
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
    remaining_friday_capacity = sum(max(0, FRIDAY_LIMIT - item.friday_count) for item in base_ledgers)
    fixed_friday_slots = sum(1 for (duty_date, _period) in fixed if duty_date.weekday() == 4)
    required_slots = friday_days * 2
    if remaining_friday_capacity < required_slots:
        raise ValueError(
            "لا يمكن توزيع كل جمعات هذه الفترة ضمن حد جمعتين لكل صيدلية. "
            f"المطلوب {required_slots} خانات جمعة والمتاح {remaining_friday_capacity}."
        )
    if fixed_friday_slots:
        # Fixed slots are included in the required capacity; this branch exists to make malformed
        # duplicate fixed data fail during analysis instead of silently increasing the annual limit.
        pass

    best_assignments: list[SmartAssignment] | None = None
    best_analysis: ScheduleAnalysis | None = None
    base_seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31 - 1)
    for attempt in range(max(1, attempts)):
        rng = random.Random(base_seed + attempt * 104729)
        assignments = _generate_once(
            base_ledgers,
            start_date=start_date,
            end_date=end_date,
            times=times,
            timezone=timezone,
            fixed=fixed,
            rng=rng,
        )
        analysis = analyze_assignments(assignments, base_ledgers)
        if best_analysis is None or analysis.penalty < best_analysis.penalty:
            best_assignments = assignments
            best_analysis = analysis
        if analysis.penalty == 0:
            break
    if best_assignments is None or best_analysis is None:
        raise ValueError("تعذر إنشاء جدول مناسب.")
    return best_assignments, best_analysis


async def _active_pharmacies_and_shifts(session: AsyncSession) -> tuple[list[Pharmacy], list[Shift]]:
    pharmacies = list(
        await session.scalars(
            select(Pharmacy)
            .where(
                Pharmacy.status == "active",
                Pharmacy.deleted_at.is_(None),
            )
            .order_by(Pharmacy.name)
        )
    )
    shifts = list(
        await session.scalars(
            select(Shift)
            .options(selectinload(Shift.pharmacy))
            .join(Shift.pharmacy)
            .where(
                Shift.active.is_(True),
                Pharmacy.status == "active",
                Pharmacy.deleted_at.is_(None),
            )
            .order_by(Shift.start_at, Shift.id)
        )
    )
    return pharmacies, shifts


async def generate_import_rows(
    session: AsyncSession,
    *,
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
    times: ShiftTimes,
    fixed: Mapping[tuple[date, str], int] | None = None,
) -> tuple[list[dict[str, Any]], ScheduleAnalysis]:
    pharmacies, shifts = await _active_pharmacies_and_shifts(session)
    ledgers = _build_ledgers(
        pharmacies,
        shifts,
        year=start_date.year,
        timezone=timezone,
        before_date=start_date,
    )
    assignments, analysis = generate_best_schedule(
        ledgers,
        start_date=start_date,
        end_date=end_date,
        times=times,
        timezone=timezone,
        fixed=fixed,
    )
    rows: list[dict[str, Any]] = []
    for row_number, item in enumerate(assignments, start=1):
        rows.append(
            {
                "row_number": row_number,
                "raw_pharmacy_name": item.pharmacy_name,
                "matched_pharmacy_id": item.pharmacy_id,
                "start_at": item.start_at,
                "end_at": item.end_at,
                "confidence": 100.0,
                "status": "ready",
                "errors": [],
                "raw_data": {
                    "smart": True,
                    "period": item.period,
                    "locked": item.locked,
                    "algorithm": SMART_ALGORITHM,
                },
            }
        )
    return rows, analysis


async def analyze_batch(session: AsyncSession, batch: ImportBatch, timezone: ZoneInfo) -> ScheduleAnalysis:
    if batch.period_start is None:
        raise ValueError("المسودة لا تحتوي تاريخ بداية.")
    pharmacies, shifts = await _active_pharmacies_and_shifts(session)
    ledgers = _build_ledgers(
        pharmacies,
        shifts,
        year=batch.period_start.year,
        timezone=timezone,
        before_date=batch.period_start,
    )
    assignments: list[SmartAssignment] = []
    for row in batch.rows:
        if not row.matched_pharmacy_id or not row.start_at or not row.end_at:
            continue
        name = row.matched_pharmacy.name if row.matched_pharmacy else row.raw_pharmacy_name
        period = str((row.raw_data or {}).get("period") or _period_for_start(row.start_at, timezone))
        assignments.append(
            SmartAssignment(
                duty_date=as_local(row.start_at, timezone).date(),
                period=period,
                pharmacy_id=row.matched_pharmacy_id,
                pharmacy_name=name,
                start_at=row.start_at,
                end_at=row.end_at,
                locked=bool((row.raw_data or {}).get("locked")),
            )
        )
    return analyze_assignments(assignments, ledgers)


def fixed_from_batch(batch: ImportBatch, timezone: ZoneInfo) -> dict[tuple[date, str], int]:
    fixed: dict[tuple[date, str], int] = {}
    for row in batch.rows:
        if not row.matched_pharmacy_id or not row.start_at or not (row.raw_data or {}).get("locked"):
            continue
        period = str((row.raw_data or {}).get("period") or _period_for_start(row.start_at, timezone))
        fixed[(as_local(row.start_at, timezone).date(), period)] = row.matched_pharmacy_id
    return fixed


def draft_shift_views(batch: ImportBatch) -> list[DraftShift]:
    result: list[DraftShift] = []
    for row in batch.rows:
        if not row.start_at or not row.end_at or not row.matched_pharmacy:
            continue
        result.append(
            DraftShift(
                start_at=row.start_at,
                end_at=row.end_at,
                pharmacy=SimpleNamespace(name=row.matched_pharmacy.name),
            )
        )
    return result


async def pharmacy_year_statistics(
    session: AsyncSession,
    *,
    year: int,
    timezone: ZoneInfo,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pharmacies, shifts = await _active_pharmacies_and_shifts(session)
    now = now or utcnow()
    rows: dict[int, dict[str, Any]] = {
        pharmacy.id: {
            "id": pharmacy.id,
            "name": pharmacy.name,
            "total": 0,
            "day": 0,
            "evening": 0,
            "friday_dates": set(),
            "last": None,
            "next": None,
            "months": Counter(),
        }
        for pharmacy in pharmacies
    }
    for shift in shifts:
        item = rows.get(shift.pharmacy_id)
        if item is None:
            continue
        local = as_local(shift.start_at, timezone)
        duty_date = local.date()
        if shift.start_at <= now and (item["last"] is None or shift.start_at > item["last"].start_at):
            item["last"] = shift
        if shift.start_at > now and (item["next"] is None or shift.start_at < item["next"].start_at):
            item["next"] = shift
        if duty_date.year != year:
            continue
        item["total"] += 1
        item["months"][duty_date.month] += 1
        if _period_for_start(shift.start_at, timezone) == DAY:
            item["day"] += 1
        else:
            item["evening"] += 1
        if duty_date.weekday() == 4:
            item["friday_dates"].add(duty_date)

    result: list[dict[str, Any]] = []
    for item in rows.values():
        friday_dates = sorted(item["friday_dates"])
        result.append(
            {
                **item,
                "friday_dates": friday_dates,
                "fridays": len(friday_dates),
                "months": dict(item["months"]),
            }
        )
    result.sort(key=lambda item: (item["total"], item["name"]))
    totals = [item["total"] for item in result]
    summary = {
        "year": year,
        "pharmacies": len(result),
        "total": sum(totals),
        "day": sum(item["day"] for item in result),
        "evening": sum(item["evening"] for item in result),
        "friday_assignments": sum(item["fridays"] for item in result),
        "min_total": min(totals) if totals else 0,
        "max_total": max(totals) if totals else 0,
        "spread": (max(totals) - min(totals)) if totals else 0,
        "average": (sum(totals) / len(totals)) if totals else 0.0,
    }
    return result, summary
