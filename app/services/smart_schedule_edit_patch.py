from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from app.services import smart_schedule as smart
from app.utils import as_local


class FixedAssignments(dict[tuple[date, str], int]):
    """Locked smart assignments plus their original generated choices."""

    def __init__(self) -> None:
        super().__init__()
        self.generated_origins: dict[tuple[date, str], tuple[int, str | None]] = {}
        self.manual_keys: set[tuple[date, str]] = set()


_ORIGINAL_GENERATE_IMPORT_ROWS = smart.generate_import_rows


def fixed_from_batch(batch, timezone: ZoneInfo) -> FixedAssignments:
    fixed = FixedAssignments()
    for row in batch.rows:
        data = dict(row.raw_data or {})
        if not row.matched_pharmacy_id or not row.start_at or not data.get("locked"):
            continue
        period = str(data.get("period") or smart._period_for_start(row.start_at, timezone))
        key = (as_local(row.start_at, timezone).date(), period)
        fixed[key] = row.matched_pharmacy_id

        if data.get("manual_override"):
            fixed.manual_keys.add(key)
            original_id = data.get("generated_pharmacy_id")
            if original_id is not None:
                try:
                    original_id = int(original_id)
                except (TypeError, ValueError):
                    original_id = None
            if original_id:
                fixed.generated_origins[key] = (
                    original_id,
                    str(data.get("generated_pharmacy_name") or "") or None,
                )
    return fixed


async def generate_import_rows(
    session,
    *,
    start_date,
    end_date,
    timezone,
    times,
    fixed=None,
):
    rows, analysis = await _ORIGINAL_GENERATE_IMPORT_ROWS(
        session,
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        times=times,
        fixed=fixed,
    )

    fixed_meta = fixed if isinstance(fixed, FixedAssignments) else None
    for row in rows:
        data: dict[str, Any] = dict(row.get("raw_data") or {})
        period = str(data.get("period") or smart._period_for_start(row["start_at"], timezone))
        key = (as_local(row["start_at"], timezone).date(), period)

        if fixed_meta and key in fixed_meta.manual_keys:
            data["manual_override"] = True
            origin = fixed_meta.generated_origins.get(key)
            if origin:
                data["generated_pharmacy_id"] = origin[0]
                if origin[1]:
                    data["generated_pharmacy_name"] = origin[1]
        else:
            data.setdefault("generated_pharmacy_id", row.get("matched_pharmacy_id"))
            data.setdefault("generated_pharmacy_name", row.get("raw_pharmacy_name"))

        row["raw_data"] = data
    return rows, analysis


# Deliberately do not replace app.services.smart_schedule functions globally.
# The smart-schedule Telegram workflow installs these wrappers locally after its
# original handlers are imported. This preserves the proven service integration
# contract while adding reversible edit metadata only where it is needed.
