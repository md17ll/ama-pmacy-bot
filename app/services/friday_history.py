from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.models import Pharmacy
from app.utils import normalize_text


REFERENCE_YEAR = 2026
REFERENCE_THROUGH = date(2026, 8, 14)
REFERENCE_SOURCES = (
    "1000327660.jpg",
    "1000327670.jpg",
    "1000327635.jpg",
    "1000327643.jpg",
)

# The two notebook pages contain the dated Friday pair history.  Pair order is
# intentionally NOT interpreted as daytime/evening because the handwriting
# only establishes that both pharmacies were assigned that Friday.
FRIDAY_PAIRS_2026: tuple[tuple[date, str, str], ...] = (
    (date(2026, 1, 2), "عامودا", "سيدو"),
    (date(2026, 1, 9), "رشاد", "افا"),
    (date(2026, 1, 16), "دقوري", "سوزان"),
    (date(2026, 1, 23), "حسن", "مؤيد"),
    (date(2026, 1, 30), "مروى", "روان"),
    (date(2026, 2, 6), "نور", "هسو"),
    (date(2026, 2, 13), "شيرين", "ليلان"),
    (date(2026, 2, 20), "عصام", "زنار"),
    (date(2026, 2, 27), "هيزل", "افا"),
    (date(2026, 3, 6), "سيدو", "نور"),
    (date(2026, 3, 13), "مؤيد", "محمد حسو"),
    (date(2026, 3, 20), "روان", "فواز"),
    (date(2026, 3, 27), "سعد", "جيهان"),
    (date(2026, 4, 3), "مايا", "علي"),
    (date(2026, 4, 10), "ابراهيم", "روان"),
    (date(2026, 4, 17), "افا", "رجب"),
    (date(2026, 4, 24), "هسو", "لمار"),
    (date(2026, 5, 1), "نور", "يوسف"),
    (date(2026, 5, 8), "فواز", "سوزان"),
    (date(2026, 5, 15), "عامر", "هيلين"),
    (date(2026, 5, 22), "ليلان", "بيمان"),
    (date(2026, 5, 29), "هيلين", "عامودا"),
    (date(2026, 6, 5), "لمار", "اهين"),
    (date(2026, 6, 12), "بيمان", "رمضان"),
    (date(2026, 6, 19), "فارس", "محمد نور"),
    (date(2026, 6, 26), "شيرين", "كاميران"),
    (date(2026, 7, 3), "روان", "دباغ"),
    (date(2026, 7, 10), "مروى", "اهين"),
    (date(2026, 7, 17), "شمس", "هيزل"),
    (date(2026, 7, 24), "علي", "مايا"),
    (date(2026, 7, 31), "سيدو", "حسن"),
    (date(2026, 8, 7), "عصام", "محمد حسو"),
    (date(2026, 8, 14), "رشاد", "زنار"),
)

# The two count pages are kept as a minimum-credit reference.  They show 1/2
# or 2/2 but do not provide a date for every credit.  A floor prevents a
# pharmacy from losing an already-recorded Friday just because its exact date
# is absent from the dated pages.  Exact dated records can legitimately make
# the real historical count higher than this capped 1/2–2/2 reference.
FRIDAY_COUNT_FLOORS_2026: dict[str, int] = {
    "يوسف": 1,
    "مروى": 2,
    "محمد حسو": 2,
    "لمار": 2,
    "اهين": 2,
    "عصام": 2,
    "حسن": 2,
    "هسو": 2,
    "رشاد": 2,
    "دباغ": 1,
    "رمضان": 1,
    "عامودا": 2,
    "شمس": 2,
    "شيرين": 2,
    "سيدو": 2,
    "ابراهيم": 1,
    "مؤيد": 2,
    "فواز": 2,
    "رجب": 1,
    "مايا": 2,
    "زنار": 2,
    "روان": 2,
    "كاميران": 1,
    "افا": 2,
    "نور": 2,
    "هيلين": 2,
    "علي": 2,
    "محمد نور": 1,
    "سعد": 2,
    "بيمان": 2,
    "ليلان": 2,
    "سوزان": 1,
    "هيزل": 1,
    "دقوري": 1,
    "عامر": 1,
    "فارس": 1,
}


@dataclass(frozen=True, slots=True)
class FridayHistoryCredit:
    dates: frozenset[date]
    floor: int


def compact_pharmacy_key(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def _pharmacy_keys(pharmacy: Pharmacy) -> set[str]:
    keys = {compact_pharmacy_key(pharmacy.name)}
    for alias in getattr(pharmacy, "aliases", ()) or ():
        alias_text = getattr(alias, "alias", "")
        if alias_text:
            keys.add(compact_pharmacy_key(alias_text))
    return {key for key in keys if key}


def friday_history_for_pharmacies(
    pharmacies: Iterable[Pharmacy],
    *,
    year: int,
    before_date: date | None = None,
) -> dict[int, FridayHistoryCredit]:
    """Match the user-provided Friday reference to current pharmacy records.

    Exact dates are included only before ``before_date`` when provided, which
    prevents future reference rows from leaking into an earlier back-test.  The
    undated 1/2–2/2 floors are applied only once the requested period is after
    the reference-through date.  For later years the annual ledger resets.
    """
    if year != REFERENCE_YEAR:
        return {}

    pharmacy_list = list(pharmacies)
    key_to_id: dict[str, int] = {}
    for pharmacy in pharmacy_list:
        for key in _pharmacy_keys(pharmacy):
            key_to_id.setdefault(key, pharmacy.id)

    dates_by_id: dict[int, set[date]] = {pharmacy.id: set() for pharmacy in pharmacy_list}
    for duty_date, first_name, second_name in FRIDAY_PAIRS_2026:
        if before_date is not None and duty_date >= before_date:
            continue
        for name in (first_name, second_name):
            pharmacy_id = key_to_id.get(compact_pharmacy_key(name))
            if pharmacy_id is not None:
                dates_by_id[pharmacy_id].add(duty_date)

    apply_floors = before_date is None or before_date > REFERENCE_THROUGH
    floors_by_id: dict[int, int] = {pharmacy.id: 0 for pharmacy in pharmacy_list}
    if apply_floors:
        for name, floor in FRIDAY_COUNT_FLOORS_2026.items():
            pharmacy_id = key_to_id.get(compact_pharmacy_key(name))
            if pharmacy_id is not None:
                floors_by_id[pharmacy_id] = max(floors_by_id[pharmacy_id], int(floor))

    return {
        pharmacy.id: FridayHistoryCredit(
            dates=frozenset(dates_by_id[pharmacy.id]),
            floor=floors_by_id[pharmacy.id],
        )
        for pharmacy in pharmacy_list
        if dates_by_id[pharmacy.id] or floors_by_id[pharmacy.id]
    }


def reference_summary() -> dict[str, int | str]:
    return {
        "year": REFERENCE_YEAR,
        "through": REFERENCE_THROUGH.isoformat(),
        "dated_fridays": len(FRIDAY_PAIRS_2026),
        "dated_assignments": len(FRIDAY_PAIRS_2026) * 2,
        "count_floor_pharmacies": len(FRIDAY_COUNT_FLOORS_2026),
        "sources": len(REFERENCE_SOURCES),
    }
