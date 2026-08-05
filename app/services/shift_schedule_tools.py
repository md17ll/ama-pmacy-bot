from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, BotSetting, Shift
from app.utils import as_local, combine_shift, utcnow


DAY_START_KEY = "shift_day_start"
DAY_END_KEY = "shift_day_end"
EVENING_START_KEY = "shift_evening_start"
EVENING_END_KEY = "shift_evening_end"

DEFAULT_DAY_START = time(13, 30)
DEFAULT_DAY_END = time(17, 0)
DEFAULT_EVENING_START = time(20, 30)
DEFAULT_EVENING_END = time(23, 30)


@dataclass(frozen=True, slots=True)
class ShiftTimes:
    day_start: time = DEFAULT_DAY_START
    day_end: time = DEFAULT_DAY_END
    evening_start: time = DEFAULT_EVENING_START
    evening_end: time = DEFAULT_EVENING_END

    def as_dict(self) -> dict[str, str]:
        return {
            "day_start": _clock_text(self.day_start),
            "day_end": _clock_text(self.day_end),
            "evening_start": _clock_text(self.evening_start),
            "evening_end": _clock_text(self.evening_end),
        }


_SETTING_DEFAULTS: tuple[tuple[str, time], ...] = (
    (DAY_START_KEY, DEFAULT_DAY_START),
    (DAY_END_KEY, DEFAULT_DAY_END),
    (EVENING_START_KEY, DEFAULT_EVENING_START),
    (EVENING_END_KEY, DEFAULT_EVENING_END),
)


def _clock_text(value: time) -> str:
    return value.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _parse_clock(value: object, fallback: time) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    try:
        return time.fromisoformat(str(value)).replace(tzinfo=None, second=0, microsecond=0)
    except (TypeError, ValueError):
        return fallback


def _same_instant(value: datetime, serialized: object) -> bool:
    try:
        expected = datetime.fromisoformat(str(serialized))
    except (TypeError, ValueError):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) == expected.astimezone(timezone.utc)


async def get_shift_times(session: AsyncSession) -> ShiftTimes:
    values: dict[str, time] = {}
    for key, fallback in _SETTING_DEFAULTS:
        setting = await session.get(BotSetting, key)
        values[key] = _parse_clock(setting.value if setting else None, fallback)
    return ShiftTimes(
        day_start=values[DAY_START_KEY],
        day_end=values[DAY_END_KEY],
        evening_start=values[EVENING_START_KEY],
        evening_end=values[EVENING_END_KEY],
    )


def validate_shift_times(times: ShiftTimes) -> None:
    anchor = datetime(2026, 1, 1)
    day_start = datetime.combine(anchor.date(), times.day_start)
    day_end = datetime.combine(anchor.date(), times.day_end)
    evening_start = datetime.combine(anchor.date(), times.evening_start)
    evening_end = datetime.combine(anchor.date(), times.evening_end)
    if day_end <= day_start:
        raise ValueError("نهاية المناوبة النهارية يجب أن تكون بعد بدايتها.")
    if evening_end <= evening_start:
        evening_end = evening_end.replace(day=2)
    if day_end > evening_start:
        raise ValueError("وقت المناوبة النهارية يجب أن ينتهي قبل بداية المسائية.")
    if (day_end - day_start).total_seconds() > 12 * 3600:
        raise ValueError("مدة المناوبة النهارية طويلة بشكل غير صالح.")
    if (evening_end - evening_start).total_seconds() > 12 * 3600:
        raise ValueError("مدة المناوبة المسائية طويلة بشكل غير صالح.")


def _period_for_shift(shift: Shift, timezone: ZoneInfo) -> str:
    local_start = as_local(shift.start_at, timezone)
    return "day" if local_start.time().replace(tzinfo=None) < time(18, 0) else "evening"


def _serialize_shift(shift: Shift) -> dict[str, object]:
    return {
        "id": shift.id,
        "pharmacy_id": shift.pharmacy_id,
        "start_at": shift.start_at.isoformat(),
        "end_at": shift.end_at.isoformat(),
        "active": shift.active,
        "import_batch_id": shift.import_batch_id,
        "created_by": shift.created_by,
    }


async def _affected_shifts(session: AsyncSession, effective_at: datetime) -> list[Shift]:
    result = await session.scalars(
        select(Shift)
        .where(
            Shift.active.is_(True),
            Shift.start_at >= effective_at,
        )
        .order_by(Shift.start_at, Shift.id)
    )
    return list(result)


async def count_affected_shifts(session: AsyncSession, effective_at: datetime) -> int:
    return len(await _affected_shifts(session, effective_at))


async def _set_setting(session: AsyncSession, key: str, value: str) -> None:
    setting = await session.get(BotSetting, key)
    if setting is None:
        session.add(BotSetting(key=key, value=value))
    else:
        setting.value = value


async def bulk_update_shift_times(
    session: AsyncSession,
    *,
    times: ShiftTimes,
    effective_at: datetime,
    timezone: ZoneInfo,
    admin_id: int,
) -> tuple[int, int]:
    validate_shift_times(times)
    affected = await _affected_shifts(session, effective_at)
    old_settings = await get_shift_times(session)
    before = [_serialize_shift(shift) for shift in affected]

    all_active = list(
        await session.scalars(
            select(Shift).where(Shift.active.is_(True)).order_by(Shift.id)
        )
    )
    affected_ids = {shift.id for shift in affected}
    occupied = {
        (shift.pharmacy_id, shift.start_at, shift.end_at)
        for shift in all_active
        if shift.id not in affected_ids
    }
    proposed: set[tuple[int, datetime, datetime]] = set()

    for shift in affected:
        local_day = as_local(shift.start_at, timezone).date()
        if _period_for_shift(shift, timezone) == "day":
            new_start, new_end = combine_shift(
                local_day,
                times.day_start,
                times.day_end,
                timezone,
            )
        else:
            new_start, new_end = combine_shift(
                local_day,
                times.evening_start,
                times.evening_end,
                timezone,
            )
        key = (shift.pharmacy_id, new_start, new_end)
        if key in occupied or key in proposed:
            raise ValueError(
                "تعذر تطبيق الأوقات لأن هناك مناوبتين ستصبحان متطابقتين لنفس الصيدلية."
            )
        proposed.add(key)
        shift.start_at = new_start
        shift.end_at = new_end

    for key, value in (
        (DAY_START_KEY, _clock_text(times.day_start)),
        (DAY_END_KEY, _clock_text(times.day_end)),
        (EVENING_START_KEY, _clock_text(times.evening_start)),
        (EVENING_END_KEY, _clock_text(times.evening_end)),
    ):
        await _set_setting(session, key, value)

    audit = AuditLog(
        admin_id=admin_id,
        action="shift_bulk_time_update",
        entity_type="shift_schedule",
        entity_id=None,
        before_data={
            "shifts": before,
            "settings": old_settings.as_dict(),
        },
        after_data={
            "shifts": [_serialize_shift(shift) for shift in affected],
            "settings": times.as_dict(),
            "count": len(affected),
            "effective_at": effective_at.isoformat(),
        },
        reversible=False,
    )
    session.add(audit)
    try:
        await session.flush()
        audit_id = int(audit.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("حدث تعارض أثناء تعديل الأوقات، ولم يتم تغيير أي مناوبة.") from exc
    return len(affected), audit_id


async def undo_bulk_time_update(
    session: AsyncSession,
    *,
    audit_id: int,
    admin_id: int,
) -> int:
    audit = await session.get(AuditLog, audit_id)
    if (
        audit is None
        or audit.action != "shift_bulk_time_update"
        or audit.admin_id != admin_id
        or audit.reversed_at is not None
        or not audit.before_data
    ):
        raise ValueError("عملية تعديل الأوقات غير موجودة أو تم التراجع عنها مسبقاً.")

    before_rows = list(audit.before_data.get("shifts", []))
    after_rows = list((audit.after_data or {}).get("shifts", []))
    after_by_id = {int(row["id"]): row for row in after_rows if row.get("id") is not None}

    for row in before_rows:
        shift_id = int(row["id"])
        shift = await session.get(Shift, shift_id)
        if shift is None:
            continue
        expected = after_by_id.get(shift_id)
        if expected and (
            not _same_instant(shift.start_at, expected.get("start_at"))
            or not _same_instant(shift.end_at, expected.get("end_at"))
        ):
            raise ValueError(
                "لا يمكن التراجع لأن إحدى المناوبات عُدلت بعد تغيير الأوقات العامة."
            )
        shift.pharmacy_id = int(row["pharmacy_id"])
        shift.start_at = datetime.fromisoformat(str(row["start_at"]))
        shift.end_at = datetime.fromisoformat(str(row["end_at"]))
        shift.active = bool(row.get("active", True))
        shift.import_batch_id = row.get("import_batch_id")
        shift.created_by = row.get("created_by")

    old_settings = dict(audit.before_data.get("settings", {}))
    for key, field, fallback in (
        (DAY_START_KEY, "day_start", DEFAULT_DAY_START),
        (DAY_END_KEY, "day_end", DEFAULT_DAY_END),
        (EVENING_START_KEY, "evening_start", DEFAULT_EVENING_START),
        (EVENING_END_KEY, "evening_end", DEFAULT_EVENING_END),
    ):
        await _set_setting(
            session,
            key,
            _clock_text(_parse_clock(old_settings.get(field), fallback)),
        )

    audit.reversed_at = utcnow()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("تعذر التراجع بسبب تعارض جديد في المناوبات.") from exc
    return len(before_rows)
