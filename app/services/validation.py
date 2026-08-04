from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Iterable

from app.models import Shift


def validate_import_row(
    *,
    pharmacy_name: str,
    matched_pharmacy_id: int | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> list[str]:
    errors: list[str] = []
    if not pharmacy_name.strip():
        errors.append("اسم الصيدلية فارغ")
    if matched_pharmacy_id is None:
        errors.append("الصيدلية غير مطابقة مع قاعدة البيانات")
    if start_at is None:
        errors.append("تاريخ أو وقت البداية غير واضح")
    if end_at is None:
        errors.append("وقت النهاية غير واضح")
    if start_at and end_at and end_at <= start_at:
        errors.append("وقت النهاية يجب أن يكون بعد البداية")
    return errors


def detect_duplicate_rows(rows: Iterable[dict]) -> set[int]:
    keys: list[tuple[object, object, object]] = []
    indexed: list[tuple[int, tuple[object, object, object]]] = []
    for row in rows:
        key = (row.get("matched_pharmacy_id"), row.get("start_at"), row.get("end_at"))
        if all(key):
            keys.append(key)
            indexed.append((int(row["row_number"]), key))
    duplicated = {key for key, count in Counter(keys).items() if count > 1}
    return {row_number for row_number, key in indexed if key in duplicated}


def detect_shift_conflicts(shifts: Iterable[Shift]) -> list[tuple[int, int]]:
    ordered = sorted(shifts, key=lambda shift: (shift.pharmacy_id, shift.start_at, shift.end_at))
    conflicts: list[tuple[int, int]] = []
    for index, current in enumerate(ordered):
        for following in ordered[index + 1 :]:
            if following.pharmacy_id != current.pharmacy_id:
                break
            if following.start_at >= current.end_at:
                break
            if following.start_at < current.end_at and following.end_at > current.start_at:
                conflicts.append((current.id, following.id))
    return conflicts
