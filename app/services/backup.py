from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.models import Pharmacy, Shift


def build_json_backup(pharmacies: list[Pharmacy], shifts: list[Shift], timezone) -> bytes:
    payload: dict[str, Any] = {
        "version": 1,
        "generated_at": datetime.now(timezone).isoformat(),
        "timezone": str(timezone),
        "pharmacies": [
            {
                "id": pharmacy.id,
                "name": pharmacy.name,
                "address": pharmacy.address,
                "aliases": [alias.alias for alias in pharmacy.aliases],
                "status": pharmacy.status,
                "notes": pharmacy.notes,
            }
            for pharmacy in pharmacies
        ],
        "shifts": [
            {
                "id": shift.id,
                "pharmacy_id": shift.pharmacy_id,
                "start_at": shift.start_at.isoformat(),
                "end_at": shift.end_at.isoformat(),
                "active": shift.active,
            }
            for shift in shifts
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
