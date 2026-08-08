from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportBatch
from app.services import smart_schedule as smart
from app.services.friday_history import (
    FRIDAY_PAIRS_2026,
    friday_cycle_for,
    friday_history_for_pharmacies,
)
from app.services.friday_overrides import (
    build_friday_states,
    effective_dates_for_state,
    state_source_label,
)
from app.services.shift_schedule_tools import ShiftTimes
from app.utils import as_local, utcnow


_ORIGINAL_PHARMACY_YEAR_STATISTICS = smart.pharmacy_year_statistics


def _merge_reference_into_ledgers(
    pharmacies,
    ledgers,
    *,
    year: int,
    before_date: date | None,
):
    """Legacy helper used by tests: merge only the photographed reference.

    Friday quota is cycle-based (1 August to 31 July), so database dates from a
    previous cycle are removed before the photo reference is merged.
    """
    reference_date = before_date or date(year, 12, 31)
    cycle = friday_cycle_for(reference_date)
    credits = friday_history_for_pharmacies(
        pharmacies,
        year=year,
        before_date=before_date,
        reference_date=reference_date,
    )
    by_id = {ledger.pharmacy_id: ledger for ledger in ledgers}
    for ledger in ledgers:
        ledger.friday_dates = {
            duty_date
            for duty_date in ledger.friday_dates
            if cycle.start <= duty_date <= cycle.end
        }
    for pharmacy_id, credit in credits.items():
        ledger = by_id.get(pharmacy_id)
        if ledger is None:
            continue
        merged_dates = set(ledger.friday_dates) | set(credit.dates)
        quota_credit = max(len(merged_dates), credit.floor)
        kept = set(sorted(merged_dates))
        placeholder = cycle.start - timedelta(days=1)
        while len(kept) < quota_credit:
            while placeholder in kept:
                placeholder -= timedelta(days=1)
            kept.add(placeholder)
            placeholder -= timedelta(days=1)
        ledger.friday_dates = kept
    return ledgers


async def _apply_effective_cycle_state(
    session: AsyncSession,
    pharmacies,
    shifts,
    ledgers,
    *,
    reference_date: date,
    before_date: date | None,
    timezone: ZoneInfo,
):
    states = await build_friday_states(
        session,
        pharmacies,
        shifts,
        reference_date=reference_date,
        timezone=timezone,
        before_date=before_date,
    )
    for ledger in ledgers:
        state = states.get(ledger.pharmacy_id)
        ledger.friday_dates = effective_dates_for_state(state) if state else set()
    return ledgers


def _photo_overlap(start_date: date, end_date: date) -> list[date]:
    return sorted(
        duty_date
        for duty_date, _first, _second in FRIDAY_PAIRS_2026
        if start_date <= duty_date <= end_date
    )


def _published_overlap(shifts, start_date: date, end_date: date, timezone: ZoneInfo) -> list[date]:
    return sorted(
        {
            as_local(shift.start_at, timezone).date()
            for shift in shifts
            if start_date <= as_local(shift.start_at, timezone).date() <= end_date
        }
    )


def _ensure_single_friday_cycle(start_date: date, end_date: date) -> None:
    start_cycle = friday_cycle_for(start_date)
    end_cycle = friday_cycle_for(end_date)
    if start_cycle.start != end_cycle.start:
        raise ValueError(
            "الفترة تعبر بداية دورة الجمعة في 01/08. "
            "أنشئ جدولاً ينتهي في 31/07 ثم جدولاً جديداً يبدأ من 01/08 حتى يتصفّر رصيد الجمعة بشكل صحيح."
        )


async def generate_import_rows(
    session: AsyncSession,
    *,
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
    times: ShiftTimes,
    fixed: Mapping[tuple[date, str], int] | None = None,
) -> tuple[list[dict[str, Any]], smart.ScheduleAnalysis]:
    _ensure_single_friday_cycle(start_date, end_date)

    overlap = _photo_overlap(start_date, end_date)
    if overlap:
        formatted = "، ".join(value.strftime("%d/%m/%Y") for value in overlap)
        raise ValueError(
            "الفترة تتداخل مع جمعات مثبتة في الصور الأصلية "
            f"({formatted}). ابدأ الجدول بعد آخر تاريخ مصوّر أو عدّل الفترة."
        )

    pharmacies, shifts = await smart._active_pharmacies_and_shifts(session)
    published_overlap = _published_overlap(shifts, start_date, end_date, timezone)
    if published_overlap:
        formatted = "، ".join(value.strftime("%d/%m/%Y") for value in published_overlap[:6])
        suffix = "…" if len(published_overlap) > 6 else ""
        raise ValueError(
            "الفترة تتداخل مع مناوبات منشورة مسبقاً "
            f"({formatted}{suffix}). ابدأ بعد آخر يوم منشور لتجنب تكرار المناوبات."
        )

    ledgers = smart._build_ledgers(
        pharmacies,
        shifts,
        year=start_date.year,
        timezone=timezone,
        before_date=start_date,
    )
    ledgers = await _apply_effective_cycle_state(
        session,
        pharmacies,
        shifts,
        ledgers,
        reference_date=start_date,
        before_date=start_date,
        timezone=timezone,
    )
    assignments, analysis = smart.generate_best_schedule(
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
                    "algorithm": smart.SMART_ALGORITHM,
                    "friday_history": "photo+database+manual-cycle",
                    "friday_cycle": friday_cycle_for(start_date).key,
                },
            }
        )
    return rows, analysis


async def analyze_batch(
    session: AsyncSession,
    batch: ImportBatch,
    timezone: ZoneInfo,
) -> smart.ScheduleAnalysis:
    if batch.period_start is None:
        raise ValueError("المسودة لا تحتوي تاريخ بداية.")
    pharmacies, shifts = await smart._active_pharmacies_and_shifts(session)
    ledgers = smart._build_ledgers(
        pharmacies,
        shifts,
        year=batch.period_start.year,
        timezone=timezone,
        before_date=batch.period_start,
    )
    ledgers = await _apply_effective_cycle_state(
        session,
        pharmacies,
        shifts,
        ledgers,
        reference_date=batch.period_start,
        before_date=batch.period_start,
        timezone=timezone,
    )
    assignments: list[smart.SmartAssignment] = []
    for row in batch.rows:
        if not row.matched_pharmacy_id or not row.start_at or not row.end_at:
            continue
        name = row.matched_pharmacy.name if row.matched_pharmacy else row.raw_pharmacy_name
        period = str(
            (row.raw_data or {}).get("period")
            or smart._period_for_start(row.start_at, timezone)
        )
        assignments.append(
            smart.SmartAssignment(
                duty_date=as_local(row.start_at, timezone).date(),
                period=period,
                pharmacy_id=row.matched_pharmacy_id,
                pharmacy_name=name,
                start_at=row.start_at,
                end_at=row.end_at,
                locked=bool((row.raw_data or {}).get("locked")),
            )
        )
    return smart.analyze_assignments(assignments, ledgers)


async def pharmacy_year_statistics(
    session: AsyncSession,
    *,
    year: int,
    timezone: ZoneInfo,
    now=None,
):
    items, summary = await _ORIGINAL_PHARMACY_YEAR_STATISTICS(
        session,
        year=year,
        timezone=timezone,
        now=now,
    )
    pharmacies, shifts = await smart._active_pharmacies_and_shifts(session)
    local_now = as_local(now or utcnow(), timezone).date()
    reference_date = local_now if local_now.year == year else date(year, 12, 31)
    states = await build_friday_states(
        session,
        pharmacies,
        shifts,
        reference_date=reference_date,
        timezone=timezone,
        before_date=None,
    )

    for item in items:
        state = states.get(item["id"])
        if state is None:
            item["fridays"] = 0
            item["friday_dates"] = []
            item["friday_reference_floor"] = 0
            item["friday_override"] = None
            continue
        real_dates = sorted(set(state.image_dates) | set(state.database_dates))
        item["friday_dates"] = real_dates
        item["friday_recorded"] = len(real_dates)
        item["friday_reference_floor"] = state.reference_floor
        item["friday_override"] = state.override_count
        item["fridays"] = state.effective_count
        item["friday_cycle_start"] = state.cycle.start
        item["friday_cycle_end"] = state.cycle.end
        item["friday_source"] = state_source_label(state)

    summary["friday_assignments"] = sum(item["fridays"] for item in items)
    summary["friday_cycle_start"] = friday_cycle_for(reference_date).start
    summary["friday_cycle_end"] = friday_cycle_for(reference_date).end
    summary["friday_reference_loaded"] = any(state.image_dates or state.reference_floor for state in states.values())
    summary["friday_override_count"] = sum(1 for state in states.values() if state.is_overridden)
    return items, summary


# Production handlers import these names from app.services.smart_schedule after
# this module is loaded by app.handlers.__init__. Replacing only these integration
# points keeps the proven scheduling algorithm unchanged.
smart.generate_import_rows = generate_import_rows
smart.analyze_batch = analyze_batch
smart.pharmacy_year_statistics = pharmacy_year_statistics
