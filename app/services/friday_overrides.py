from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, BotSetting, Pharmacy, Shift
from app.services.friday_history import (
    FRIDAY_PAIRS_2026,
    FridayCycle,
    compact_pharmacy_key,
    friday_cycle_for,
    friday_history_for_pharmacies,
)
from app.utils import as_local, utcnow


FRIDAY_OVERRIDE_SETTING = "smart_friday_cycle_overrides_v1"
FRIDAY_LIMIT = 2


@dataclass(frozen=True, slots=True)
class FridayPharmacyState:
    pharmacy_id: int
    name: str
    cycle: FridayCycle
    image_dates: tuple[date, ...]
    database_dates: tuple[date, ...]
    reference_floor: int
    base_count: int
    override_count: int | None
    effective_count: int

    @property
    def is_overridden(self) -> bool:
        return self.override_count is not None


def _parse_overrides(raw: Any, cycle: FridayCycle) -> dict[int, int]:
    if not isinstance(raw, dict):
        return {}
    cycle_data = raw.get(cycle.key)
    if not isinstance(cycle_data, dict):
        return {}
    result: dict[int, int] = {}
    for raw_id, raw_count in cycle_data.items():
        try:
            pharmacy_id = int(raw_id)
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if 0 <= count <= FRIDAY_LIMIT:
            result[pharmacy_id] = count
    return result


async def load_friday_overrides(session: AsyncSession, cycle: FridayCycle) -> dict[int, int]:
    setting = await session.get(BotSetting, FRIDAY_OVERRIDE_SETTING)
    return _parse_overrides(setting.value if setting else None, cycle)


async def _save_override_value(
    session: AsyncSession,
    *,
    cycle: FridayCycle,
    pharmacy_id: int,
    count: int | None,
    admin_id: int,
    minimum_count: int = 0,
) -> None:
    pharmacy = await session.get(Pharmacy, pharmacy_id)
    if pharmacy is None or pharmacy.deleted_at is not None or pharmacy.status != "active":
        raise ValueError("الصيدلية غير موجودة أو غير فعالة.")
    if count is not None and not 0 <= count <= FRIDAY_LIMIT:
        raise ValueError("رصيد الجمعة يجب أن يكون بين 0 و2.")
    if count is not None and count < minimum_count:
        raise ValueError(
            f"لا يمكن ضبط الرصيد على {count}/2 لأن هناك {minimum_count} جمعة منشورة فعلياً لهذه الصيدلية في الدورة الحالية."
        )

    setting = await session.get(BotSetting, FRIDAY_OVERRIDE_SETTING)
    raw: dict[str, Any] = dict(setting.value) if setting and isinstance(setting.value, dict) else {}
    cycle_data = dict(raw.get(cycle.key)) if isinstance(raw.get(cycle.key), dict) else {}
    before = cycle_data.get(str(pharmacy_id))

    if count is None:
        cycle_data.pop(str(pharmacy_id), None)
    else:
        cycle_data[str(pharmacy_id)] = int(count)

    if cycle_data:
        raw[cycle.key] = cycle_data
    else:
        raw.pop(cycle.key, None)

    if setting is None:
        session.add(BotSetting(key=FRIDAY_OVERRIDE_SETTING, value=raw))
    else:
        setting.value = raw

    session.add(
        AuditLog(
            admin_id=admin_id,
            action="smart_friday_override",
            entity_type="pharmacy",
            entity_id=str(pharmacy_id),
            before_data={"cycle": cycle.key, "count": before},
            after_data={"cycle": cycle.key, "count": count},
            reversible=False,
        )
    )
    await session.commit()


async def set_friday_override(
    session: AsyncSession,
    *,
    reference_date: date,
    pharmacy_id: int,
    count: int,
    admin_id: int,
    minimum_count: int = 0,
) -> None:
    await _save_override_value(
        session,
        cycle=friday_cycle_for(reference_date),
        pharmacy_id=pharmacy_id,
        count=count,
        admin_id=admin_id,
        minimum_count=minimum_count,
    )


async def clear_friday_override(
    session: AsyncSession,
    *,
    reference_date: date,
    pharmacy_id: int,
    admin_id: int,
) -> None:
    await _save_override_value(
        session,
        cycle=friday_cycle_for(reference_date),
        pharmacy_id=pharmacy_id,
        count=None,
        admin_id=admin_id,
    )


def _database_fridays(
    pharmacies: Iterable[Pharmacy],
    shifts: Iterable[Shift],
    *,
    cycle: FridayCycle,
    timezone: ZoneInfo,
    before_date: date | None,
) -> dict[int, set[date]]:
    pharmacy_ids = {pharmacy.id for pharmacy in pharmacies}
    result: dict[int, set[date]] = {pharmacy_id: set() for pharmacy_id in pharmacy_ids}
    for shift in shifts:
        if not shift.active or shift.pharmacy_id not in result:
            continue
        duty_date = as_local(shift.start_at, timezone).date()
        if not (cycle.start <= duty_date <= cycle.end):
            continue
        if before_date is not None and duty_date >= before_date:
            continue
        if duty_date.weekday() == 4:
            result[shift.pharmacy_id].add(duty_date)
    return result


def unmatched_photo_names(
    pharmacies: Iterable[Pharmacy],
    *,
    reference_date: date,
    before_date: date | None = None,
) -> list[str]:
    """Return photographed names in this cycle that match no active pharmacy/alias."""
    cycle = friday_cycle_for(reference_date)
    known: set[str] = set()
    for pharmacy in pharmacies:
        known.add(compact_pharmacy_key(pharmacy.name))
        for alias in getattr(pharmacy, "aliases", ()) or ():
            value = getattr(alias, "alias", "")
            if value:
                known.add(compact_pharmacy_key(value))

    missing: set[str] = set()
    for duty_date, first_name, second_name in FRIDAY_PAIRS_2026:
        if not (cycle.start <= duty_date <= cycle.end):
            continue
        if before_date is not None and duty_date >= before_date:
            continue
        for name in (first_name, second_name):
            if compact_pharmacy_key(name) not in known:
                missing.add(name)
    return sorted(missing)


async def build_friday_states(
    session: AsyncSession,
    pharmacies: Iterable[Pharmacy],
    shifts: Iterable[Shift],
    *,
    reference_date: date,
    timezone: ZoneInfo,
    before_date: date | None = None,
) -> dict[int, FridayPharmacyState]:
    pharmacy_list = list(pharmacies)
    shift_list = list(shifts)
    cycle = friday_cycle_for(reference_date)
    image = friday_history_for_pharmacies(
        pharmacy_list,
        year=reference_date.year,
        before_date=before_date,
        reference_date=reference_date,
    )
    database = _database_fridays(
        pharmacy_list,
        shift_list,
        cycle=cycle,
        timezone=timezone,
        before_date=before_date,
    )
    overrides = await load_friday_overrides(session, cycle)

    result: dict[int, FridayPharmacyState] = {}
    for pharmacy in pharmacy_list:
        credit = image.get(pharmacy.id)
        image_dates = set(credit.dates) if credit else set()
        database_dates = database.get(pharmacy.id, set())
        merged_dates = image_dates | database_dates
        floor = int(credit.floor) if credit else 0
        base_count = max(len(merged_dates), floor)
        override = overrides.get(pharmacy.id)
        effective = override if override is not None else base_count
        result[pharmacy.id] = FridayPharmacyState(
            pharmacy_id=pharmacy.id,
            name=pharmacy.name,
            cycle=cycle,
            image_dates=tuple(sorted(image_dates)),
            database_dates=tuple(sorted(database_dates)),
            reference_floor=floor,
            base_count=base_count,
            override_count=override,
            effective_count=effective,
        )
    return result


def effective_dates_for_state(state: FridayPharmacyState) -> set[date]:
    """Return a synthetic set whose size equals the effective Friday quota count."""
    count = max(0, int(state.effective_count))
    real_dates = sorted(set(state.image_dates) | set(state.database_dates))
    kept = set(real_dates[:count])
    placeholder = state.cycle.start - timedelta(days=1)
    while len(kept) < count:
        while placeholder in kept:
            placeholder -= timedelta(days=1)
        kept.add(placeholder)
        placeholder -= timedelta(days=1)
    return kept


def state_source_label(state: FridayPharmacyState) -> str:
    if state.is_overridden:
        return "تعديل يدوي"
    if state.image_dates and state.database_dates:
        return "الصورة + الجدول المنشور"
    if state.image_dates or state.reference_floor:
        return "مرجع الصورة"
    if state.database_dates:
        return "الجدول المنشور"
    return "بدون سجل"


def current_reference_date(timezone: ZoneInfo) -> date:
    return as_local(utcnow(), timezone).date()
