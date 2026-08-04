from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.services.validation import detect_duplicate_rows, validate_import_row
from app.utils import ParsedShift, combine_shift


async def prepare_import_rows(
    session: AsyncSession,
    parsed_rows: list[ParsedShift],
    timezone,
) -> list[dict]:
    rows: list[dict] = []
    for parsed in parsed_rows:
        start_at = None
        end_at = None
        errors: list[str] = []
        try:
            start_at, end_at = combine_shift(
                parsed.duty_date,
                parsed.start_time,
                parsed.end_time,
                timezone,
            )
        except ValueError as exc:
            errors.append(str(exc))

        pharmacy, confidence = await repositories.match_pharmacy(session, parsed.pharmacy_name)
        errors.extend(
            validate_import_row(
                pharmacy_name=parsed.pharmacy_name,
                matched_pharmacy_id=pharmacy.id if pharmacy else None,
                start_at=start_at,
                end_at=end_at,
            )
        )
        rows.append(
            {
                "row_number": parsed.row_number,
                "raw_pharmacy_name": parsed.pharmacy_name,
                "matched_pharmacy_id": pharmacy.id if pharmacy else None,
                "start_at": start_at,
                "end_at": end_at,
                "confidence": confidence,
                "status": "ready" if not errors else "needs_review",
                "errors": errors,
                "raw_data": parsed.raw_data,
            }
        )

    duplicate_rows = detect_duplicate_rows(rows)
    for row in rows:
        if row["row_number"] in duplicate_rows:
            row["errors"].append("السطر مكرر داخل الملف")
            row["status"] = "needs_review"
    return rows
