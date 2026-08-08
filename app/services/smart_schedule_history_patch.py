from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportBatch
from app.services import smart_schedule as smart
from app.services.friday_history import friday_history_for_pharmacies
from app.services.shift_schedule_tools import ShiftTimes
from app.utils import as_local


_ORIGINAL_PHARMACY_YEAR_STATISTICS = smart.pharmacy_year_statistics


def _merge_reference_into_ledgers(
    pharmacies,
    ledgers,
    *,
    year: int,
    before_date: date | None,
):
    """Apply the photographed Friday history as quota credit without double counting.

    The scheduler only needs the quota state 0/2, 1/2 or 2/2.  Historical
    records above two remain visible in statistics, but the internal baseline
    is capped at 2 so an old manual exception cannot make a new draft fail.
    Any newly generated third Friday still increases the set to 3 and is
    detected by the existing hard-error analysis.
    """
    credits = friday_history_for_pharmacies(
        pharmacies,
        year=year,
        before_date=before_date,
    )
    by_id = {ledger.pharmacy_id: ledger for ledger in ledgers}
    for pharmacy_id, credit in credits.items():
        ledger = by_id.get(pharmacy_id)
        if ledger is None:
            continue
        merged_dates = set(ledger.friday_dates) | set(credit.dates)
        quota_credit = min(smart.FRIDAY_LIMIT, max(len(merged_dates), credit.floor))

        # Keep up to the quota worth of real dates.  A count-page floor may be
        # higher than the dated records, so fill only the internal set with
        # harmless unique placeholders.  These placeholders are never exposed
        # to users; statistics merge the real dated source separately below.
        kept = set(sorted(merged_dates)[:quota_credit])
        placeholder = date(year, 1, 1) - timedelta(days=1)
        while len(kept) < quota_credit:
            while placeholder in kept:
                placeholder -= timedelta(days=1)
            kept.add(placeholder)
            placeholder -= timedelta(days=1)
        ledger.friday_dates = kept
    return ledgers


async def generate_import_rows(
    session: AsyncSession,
    *,
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
    times: ShiftTimes,
    fixed: Mapping[tuple[date, str], int] | None = None,
) -> tuple[list[dict[str, Any]], smart.ScheduleAnalysis]:
    pharmacies, shifts = await smart._active_pharmacies_and_shifts(session)
    ledgers = smart._build_ledgers(
        pharmacies,
        shifts,
        year=start_date.year,
        timezone=timezone,
        before_date=start_date,
    )
    ledgers = _merge_reference_into_ledgers(
        pharmacies,
        ledgers,
        year=start_date.year,
        before_date=start_date,
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
                    "friday_history": "handwritten-2026+database",
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
    ledgers = _merge_reference_into_ledgers(
        pharmacies,
        ledgers,
        year=batch.period_start.year,
        before_date=batch.period_start,
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
    pharmacies, _ = await smart._active_pharmacies_and_shifts(session)
    credits = friday_history_for_pharmacies(pharmacies, year=year, before_date=None)

    for item in items:
        credit = credits.get(item["id"])
        if credit is None:
            item["friday_recorded"] = len(item["friday_dates"])
            item["friday_reference_floor"] = 0
            continue
        merged_dates = sorted(set(item["friday_dates"]) | set(credit.dates))
        item["friday_dates"] = merged_dates
        item["friday_recorded"] = len(merged_dates)
        item["friday_reference_floor"] = credit.floor
        # UI and distribution use quota state capped at 2/2; the full dated
        # history remains available through friday_dates.
        item["fridays"] = min(
            smart.FRIDAY_LIMIT,
            max(len(merged_dates), credit.floor),
        )

    summary["friday_assignments"] = sum(item["fridays"] for item in items)
    summary["friday_reference_loaded"] = bool(credits)
    summary["friday_reference_pharmacies"] = len(credits)
    return items, summary


# Production handlers import these names from app.services.smart_schedule after
# this module is loaded by app.handlers.__init__.  Replacing only these three
# integration points keeps the proven scheduling logic unchanged.
smart.generate_import_rows = generate_import_rows
smart.analyze_batch = analyze_batch
smart.pharmacy_year_statistics = pharmacy_year_statistics
