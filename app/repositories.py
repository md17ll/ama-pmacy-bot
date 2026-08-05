from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from rapidfuzz import fuzz
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Admin,
    AuditLog,
    BotSetting,
    ImportBatch,
    ImportRow,
    Pharmacy,
    PharmacyAlias,
    Shift,
    User,
    UsageEvent,
)
from app.utils import normalize_text, utcnow


ADMIN_ROLES = {"owner", "admin", "editor", "viewer"}
WRITE_ROLES = {"owner", "admin", "editor"}
OWNER_ROLES = {"owner"}
PHARMACY_STATUSES = {"active", "temporarily_closed", "inactive"}


async def sync_owner_admins(session: AsyncSession, owner_ids: Iterable[int]) -> None:
    for owner_id in owner_ids:
        admin = await session.get(Admin, owner_id)
        if admin is None:
            session.add(
                Admin(
                    telegram_id=owner_id,
                    role="owner",
                    active=True,
                    entry_notifications=True,
                    added_by=owner_id,
                )
            )
        else:
            admin.role = "owner"
            admin.active = True
    await session.commit()


async def get_admin(session: AsyncSession, telegram_id: int) -> Admin | None:
    admin = await session.get(Admin, telegram_id)
    if admin and admin.active:
        return admin
    return None


async def is_admin(session: AsyncSession, telegram_id: int) -> bool:
    return await get_admin(session, telegram_id) is not None


async def can_write(session: AsyncSession, telegram_id: int) -> bool:
    admin = await get_admin(session, telegram_id)
    return bool(admin and admin.role in WRITE_ROLES)


async def is_owner(session: AsyncSession, telegram_id: int) -> bool:
    admin = await get_admin(session, telegram_id)
    return bool(admin and admin.role in OWNER_ROLES)


async def list_admins(session: AsyncSession) -> list[Admin]:
    result = await session.scalars(select(Admin).order_by(Admin.role, Admin.telegram_id))
    return list(result)


async def add_admin(
    session: AsyncSession,
    telegram_id: int,
    role: str,
    added_by: int,
) -> Admin:
    if role not in ADMIN_ROLES:
        raise ValueError("صلاحية الأدمن غير معروفة")
    admin = await session.get(Admin, telegram_id)
    before = None
    if admin is None:
        admin = Admin(
            telegram_id=telegram_id,
            role=role,
            active=True,
            entry_notifications=False,
            added_by=added_by,
        )
        session.add(admin)
    else:
        before = {"role": admin.role, "active": admin.active}
        admin.role = role
        admin.active = True
        admin.added_by = added_by
    session.add(
        AuditLog(
            admin_id=added_by,
            action="admin_upsert",
            entity_type="admin",
            entity_id=str(telegram_id),
            before_data=before,
            after_data={"role": role, "active": True},
            reversible=False,
        )
    )
    await session.commit()
    await session.refresh(admin)
    return admin


async def deactivate_admin(session: AsyncSession, telegram_id: int, by_admin: int) -> bool:
    admin = await session.get(Admin, telegram_id)
    if admin is None or admin.role == "owner":
        return False
    admin.active = False
    session.add(
        AuditLog(
            admin_id=by_admin,
            action="admin_deactivate",
            entity_type="admin",
            entity_id=str(telegram_id),
            before_data={"active": True, "role": admin.role},
            after_data={"active": False, "role": admin.role},
            reversible=False,
        )
    )
    await session.commit()
    return True


async def toggle_entry_notifications(session: AsyncSession, telegram_id: int) -> bool:
    admin = await session.get(Admin, telegram_id)
    if admin is None:
        raise ValueError("الأدمن غير موجود")
    admin.entry_notifications = not admin.entry_notifications
    await session.commit()
    return admin.entry_notifications


async def entry_notification_recipients(session: AsyncSession) -> list[int]:
    result = await session.scalars(
        select(Admin.telegram_id).where(Admin.active.is_(True), Admin.entry_notifications.is_(True))
    )
    return list(result)


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None,
    language_code: str | None,
    source: str | None,
) -> tuple[User, bool]:
    user = await session.get(User, telegram_id)
    is_new = user is None
    now = utcnow()
    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            first_seen_at=now,
            last_seen_at=now,
            start_count=1,
            source=source,
        )
        session.add(user)
    else:
        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        user.language_code = language_code
        user.last_seen_at = now
        user.start_count += 1
        if source and not user.source:
            user.source = source
    await session.commit()
    return user, is_new


async def user_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def create_pharmacy(
    session: AsyncSession,
    *,
    name: str,
    address: str,
    aliases: Iterable[str] = (),
    status: str = "active",
    notes: str | None = None,
    admin_id: int,
) -> Pharmacy:
    normalized = normalize_text(name)
    if not normalized:
        raise ValueError("اسم الصيدلية غير صالح")
    if status not in PHARMACY_STATUSES:
        raise ValueError("حالة الصيدلية غير صالحة")
    pharmacy = Pharmacy(
        name=name.strip(),
        normalized_name=normalized,
        address=address.strip(),
        notes=notes.strip() if notes else None,
        status=status,
    )
    seen_aliases: set[str] = set()
    for alias in aliases:
        alias = alias.strip()
        normalized_alias = normalize_text(alias)
        if (
            alias
            and normalized_alias != normalized
            and normalized_alias not in seen_aliases
        ):
            pharmacy.aliases.append(
                PharmacyAlias(alias=alias, normalized_alias=normalized_alias)
            )
            seen_aliases.add(normalized_alias)
    session.add(pharmacy)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("اسم الصيدلية موجود مسبقاً") from exc
    await session.refresh(pharmacy, attribute_names=["aliases"])
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="pharmacy_create",
            entity_type="pharmacy",
            entity_id=str(pharmacy.id),
            after_data=serialize_pharmacy(pharmacy),
            reversible=True,
        )
    )
    await session.commit()
    await session.refresh(pharmacy, attribute_names=["aliases"])
    return pharmacy


async def get_pharmacy(session: AsyncSession, pharmacy_id: int) -> Pharmacy | None:
    return await session.scalar(
        select(Pharmacy)
        .options(selectinload(Pharmacy.aliases))
        .where(Pharmacy.id == pharmacy_id, Pharmacy.deleted_at.is_(None))
    )


async def list_pharmacies(
    session: AsyncSession,
    *,
    include_inactive: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[Pharmacy]:
    query = (
        select(Pharmacy)
        .options(selectinload(Pharmacy.aliases))
        .where(Pharmacy.deleted_at.is_(None))
        .order_by(Pharmacy.name)
        .limit(limit)
        .offset(offset)
    )
    if not include_inactive:
        query = query.where(Pharmacy.status == "active")
    result = await session.scalars(query)
    return list(result)


async def pharmacy_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Pharmacy).where(Pharmacy.deleted_at.is_(None))
        )
        or 0
    )


async def update_pharmacy(
    session: AsyncSession,
    pharmacy_id: int,
    *,
    admin_id: int,
    name: str | None = None,
    address: str | None = None,
    aliases: Iterable[str] | None = None,
    status: str | None = None,
    notes: str | None = None,
) -> Pharmacy:
    pharmacy = await get_pharmacy(session, pharmacy_id)
    if pharmacy is None:
        raise ValueError("الصيدلية غير موجودة")
    before = serialize_pharmacy(pharmacy)
    if name is not None:
        normalized = normalize_text(name)
        if not normalized:
            raise ValueError("اسم الصيدلية غير صالح")
        pharmacy.name = name.strip()
        pharmacy.normalized_name = normalized
    if address is not None:
        pharmacy.address = address.strip()
    if status is not None:
        if status not in PHARMACY_STATUSES:
            raise ValueError("حالة الصيدلية غير صالحة")
        pharmacy.status = status
    if notes is not None:
        pharmacy.notes = notes.strip() or None
    if aliases is not None:
        pharmacy.aliases.clear()
        seen_aliases: set[str] = set()
        for alias in aliases:
            alias = alias.strip()
            normalized_alias = normalize_text(alias)
            if (
                alias
                and normalized_alias != pharmacy.normalized_name
                and normalized_alias not in seen_aliases
            ):
                pharmacy.aliases.append(
                    PharmacyAlias(alias=alias, normalized_alias=normalized_alias)
                )
                seen_aliases.add(normalized_alias)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ValueError("الاسم أو أحد الأسماء البديلة مستخدم مسبقاً") from exc
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="pharmacy_update",
            entity_type="pharmacy",
            entity_id=str(pharmacy.id),
            before_data=before,
            after_data=serialize_pharmacy(pharmacy),
            reversible=True,
        )
    )
    await session.commit()
    return pharmacy


async def soft_delete_pharmacy(session: AsyncSession, pharmacy_id: int, admin_id: int) -> bool:
    pharmacy = await get_pharmacy(session, pharmacy_id)
    if pharmacy is None:
        return False
    before = serialize_pharmacy(pharmacy)
    pharmacy.deleted_at = utcnow()
    pharmacy.status = "deleted"
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="pharmacy_delete",
            entity_type="pharmacy",
            entity_id=str(pharmacy.id),
            before_data=before,
            after_data={"deleted": True},
            reversible=True,
        )
    )
    await session.commit()
    return True


async def search_pharmacies(
    session: AsyncSession,
    query: str,
    limit: int = 10,
    *,
    include_inactive: bool = True,
) -> list[Pharmacy]:
    normalized = normalize_text(query)
    if not normalized:
        return []
    pharmacies = await list_pharmacies(
        session,
        include_inactive=include_inactive,
        limit=500,
    )
    scored: list[tuple[float, Pharmacy]] = []
    for pharmacy in pharmacies:
        candidates = [pharmacy.normalized_name, *(alias.normalized_alias for alias in pharmacy.aliases)]
        score = max(fuzz.WRatio(normalized, candidate) for candidate in candidates if candidate)
        if normalized in candidates:
            score = 100
        if score >= 55:
            scored.append((score, pharmacy))
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return [pharmacy for _, pharmacy in scored[:limit]]


async def match_pharmacy(
    session: AsyncSession,
    raw_name: str,
    *,
    threshold: float = 78.0,
) -> tuple[Pharmacy | None, float]:
    normalized = normalize_text(raw_name)
    if not normalized:
        return None, 0.0
    pharmacies = await list_pharmacies(session, include_inactive=False, limit=1000)
    best: tuple[float, Pharmacy | None] = (0.0, None)
    second_best = 0.0
    for pharmacy in pharmacies:
        candidates = [pharmacy.normalized_name, *(alias.normalized_alias for alias in pharmacy.aliases)]
        score = max(fuzz.WRatio(normalized, candidate) for candidate in candidates if candidate)
        if normalized in candidates:
            return pharmacy, 100.0
        if score > best[0]:
            second_best = best[0]
            best = (float(score), pharmacy)
        elif score > second_best:
            second_best = float(score)
    if best[0] >= threshold and best[0] - second_best >= 3:
        return best[1], best[0]
    return None, best[0]


async def create_shift(
    session: AsyncSession,
    *,
    pharmacy_id: int,
    start_at: datetime,
    end_at: datetime,
    admin_id: int,
    import_batch_id: int | None = None,
    log_action: bool = True,
) -> Shift:
    if end_at <= start_at:
        raise ValueError("وقت نهاية المناوبة يجب أن يكون بعد البداية")
    pharmacy = await get_pharmacy(session, pharmacy_id)
    if pharmacy is None:
        raise ValueError("الصيدلية غير موجودة")
    exact = await session.scalar(
        select(Shift).where(
            Shift.pharmacy_id == pharmacy_id,
            Shift.start_at == start_at,
            Shift.end_at == end_at,
        )
    )
    if exact and exact.active:
        raise ValueError("المناوبة مضافة مسبقاً")
    if exact:
        shift = exact
        shift.active = True
        shift.created_by = admin_id
        shift.import_batch_id = import_batch_id
    else:
        shift = Shift(
            pharmacy_id=pharmacy_id,
            start_at=start_at,
            end_at=end_at,
            created_by=admin_id,
            import_batch_id=import_batch_id,
            active=True,
        )
        session.add(shift)
        await session.flush()
    if log_action:
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="shift_create",
                entity_type="shift",
                entity_id=str(shift.id),
                after_data=serialize_shift(shift),
                reversible=True,
            )
        )
    await session.commit()
    await session.refresh(shift)
    return shift


async def get_shift(session: AsyncSession, shift_id: int) -> Shift | None:
    return await session.scalar(
        select(Shift)
        .options(selectinload(Shift.pharmacy))
        .where(Shift.id == shift_id, Shift.active.is_(True))
    )


async def list_shifts_between(
    session: AsyncSession,
    start_at: datetime,
    end_at: datetime,
    *,
    limit: int = 200,
    include_inactive_pharmacies: bool = True,
) -> list[Shift]:
    query = (
        select(Shift)
        .options(selectinload(Shift.pharmacy))
        .where(
            Shift.active.is_(True),
            Shift.start_at < end_at,
            Shift.end_at > start_at,
            Pharmacy.deleted_at.is_(None),
        )
        .join(Shift.pharmacy)
    )
    if not include_inactive_pharmacies:
        query = query.where(Pharmacy.status == "active")
    result = await session.scalars(
        query.order_by(Shift.start_at, Pharmacy.name).limit(limit)
    )
    return list(result)


async def current_shifts(session: AsyncSession, now: datetime) -> list[Shift]:
    result = await session.scalars(
        select(Shift)
        .options(selectinload(Shift.pharmacy))
        .join(Shift.pharmacy)
        .where(
            Shift.active.is_(True),
            Shift.start_at <= now,
            Shift.end_at > now,
            Pharmacy.status == "active",
            Pharmacy.deleted_at.is_(None),
        )
        .order_by(Shift.end_at, Pharmacy.name)
    )
    return list(result)


async def next_shift_for_pharmacy(
    session: AsyncSession, pharmacy_id: int, after: datetime
) -> Shift | None:
    return await session.scalar(
        select(Shift)
        .options(selectinload(Shift.pharmacy))
        .join(Shift.pharmacy)
        .where(
            Shift.pharmacy_id == pharmacy_id,
            Shift.active.is_(True),
            Shift.end_at > after,
            Pharmacy.status == "active",
            Pharmacy.deleted_at.is_(None),
        )
        .order_by(Shift.start_at)
        .limit(1)
    )


async def delete_shift(session: AsyncSession, shift_id: int, admin_id: int) -> bool:
    shift = await get_shift(session, shift_id)
    if shift is None:
        return False
    before = serialize_shift(shift)
    shift.active = False
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="shift_delete",
            entity_type="shift",
            entity_id=str(shift.id),
            before_data=before,
            after_data={"active": False},
            reversible=True,
        )
    )
    await session.commit()
    return True


async def update_shift(
    session: AsyncSession,
    shift_id: int,
    *,
    admin_id: int,
    pharmacy_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> Shift:
    shift = await get_shift(session, shift_id)
    if shift is None:
        raise ValueError("المناوبة غير موجودة")
    before = serialize_shift(shift)
    if pharmacy_id is not None:
        pharmacy = await get_pharmacy(session, pharmacy_id)
        if pharmacy is None:
            raise ValueError("الصيدلية غير موجودة")
        shift.pharmacy_id = pharmacy_id
    if start_at is not None:
        shift.start_at = start_at
    if end_at is not None:
        shift.end_at = end_at
    if shift.end_at <= shift.start_at:
        raise ValueError("وقت النهاية يجب أن يكون بعد البداية")
    duplicate = await session.scalar(
        select(Shift.id).where(
            Shift.id != shift.id,
            Shift.pharmacy_id == shift.pharmacy_id,
            Shift.start_at == shift.start_at,
            Shift.end_at == shift.end_at,
        )
    )
    if duplicate:
        raise ValueError("توجد مناوبة مطابقة بهذه البيانات")
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="shift_update",
            entity_type="shift",
            entity_id=str(shift.id),
            before_data=before,
            after_data=serialize_shift(shift),
            reversible=True,
        )
    )
    await session.commit()
    return shift


async def delete_shifts_between(
    session: AsyncSession,
    start_at: datetime,
    end_at: datetime,
    *,
    admin_id: int,
) -> int:
    shifts = await list_shifts_between(session, start_at, end_at, limit=5000)
    ids = [shift.id for shift in shifts]
    if not ids:
        return 0
    before = [serialize_shift(shift) for shift in shifts]
    await session.execute(update(Shift).where(Shift.id.in_(ids)).values(active=False))
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="shift_bulk_delete",
            entity_type="shift_period",
            entity_id=None,
            before_data={"shifts": before},
            after_data={"count": len(ids)},
            reversible=True,
        )
    )
    await session.commit()
    return len(ids)


async def shift_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Shift).where(Shift.active.is_(True))
        )
        or 0
    )


async def latest_shift_end(session: AsyncSession) -> datetime | None:
    return await session.scalar(
        select(func.max(Shift.end_at))
        .join(Shift.pharmacy)
        .where(
            Shift.active.is_(True),
            Pharmacy.status == "active",
            Pharmacy.deleted_at.is_(None),
        )
    )


async def create_import_batch(
    session: AsyncSession,
    *,
    source_type: str,
    source_name: str | None,
    source_file_id: str | None,
    created_by: int,
    rows: list[dict[str, Any]],
) -> ImportBatch:
    dates = [row["start_at"].date() for row in rows if row.get("start_at")]
    batch = ImportBatch(
        source_type=source_type,
        source_name=source_name,
        source_file_id=source_file_id,
        created_by=created_by,
        status="draft",
        period_start=min(dates) if dates else None,
        period_end=max(dates) if dates else None,
        summary={},
    )
    import_rows = [
        ImportRow(
            row_number=int(row["row_number"]),
            raw_pharmacy_name=str(row["raw_pharmacy_name"]),
            matched_pharmacy_id=row.get("matched_pharmacy_id"),
            start_at=row.get("start_at"),
            end_at=row.get("end_at"),
            confidence=row.get("confidence"),
            status=row.get("status", "pending"),
            errors=list(row.get("errors", [])),
            raw_data=dict(row.get("raw_data", {})),
        )
        for row in rows
    ]
    batch.rows = import_rows
    batch.summary = summarize_import_rows(import_rows)
    session.add(batch)
    await session.commit()
    await session.refresh(batch, attribute_names=["rows"])
    return batch


async def get_import_batch(session: AsyncSession, batch_id: int) -> ImportBatch | None:
    return await session.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows).selectinload(ImportRow.matched_pharmacy))
        .where(ImportBatch.id == batch_id)
    )


async def list_draft_batches(session: AsyncSession, limit: int = 20) -> list[ImportBatch]:
    result = await session.scalars(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows))
        .where(ImportBatch.status == "draft")
        .order_by(ImportBatch.created_at.desc())
        .limit(limit)
    )
    return list(result)


async def publish_import_batch(
    session: AsyncSession,
    batch_id: int,
    *,
    admin_id: int,
    replace_period: bool = False,
) -> tuple[int, int]:
    batch = await get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        raise ValueError("المسودة غير موجودة أو منشورة مسبقاً")
    valid_rows = [
        row
        for row in batch.rows
        if row.matched_pharmacy_id and row.start_at and row.end_at and not row.errors
    ]
    if not valid_rows:
        raise ValueError("لا توجد مناوبات صحيحة قابلة للنشر")

    removed = 0
    removed_shift_ids: list[int] = []
    if replace_period:
        min_start = min(row.start_at for row in valid_rows if row.start_at)
        max_end = max(row.end_at for row in valid_rows if row.end_at)
        existing = await list_shifts_between(session, min_start, max_end, limit=5000)
        if existing:
            removed = len(existing)
            removed_shift_ids = [shift.id for shift in existing]
            await session.execute(
                update(Shift).where(Shift.id.in_(removed_shift_ids)).values(active=False)
            )

    inserted = 0
    for row in valid_rows:
        exact = await session.scalar(
            select(Shift).where(
                Shift.pharmacy_id == row.matched_pharmacy_id,
                Shift.start_at == row.start_at,
                Shift.end_at == row.end_at,
            )
        )
        if exact and exact.active:
            row.status = "duplicate"
            continue
        if exact:
            exact.active = True
            exact.import_batch_id = batch.id
            exact.created_by = admin_id
        else:
            session.add(
                Shift(
                    pharmacy_id=row.matched_pharmacy_id,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    import_batch_id=batch.id,
                    created_by=admin_id,
                    active=True,
                )
            )
        row.status = "published"
        inserted += 1

    batch.status = "published"
    batch.published_at = utcnow()
    batch.summary = {**summarize_import_rows(batch.rows), "inserted": inserted, "removed": removed}
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="batch_publish",
            entity_type="import_batch",
            entity_id=str(batch.id),
            before_data={"status": "draft", "removed_shift_ids": removed_shift_ids},
            after_data={"inserted": inserted, "removed": removed, "replace_period": replace_period},
            reversible=True,
        )
    )
    await session.commit()
    return inserted, removed


async def cancel_import_batch(session: AsyncSession, batch_id: int, admin_id: int) -> bool:
    batch = await get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        return False
    batch.status = "cancelled"
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="batch_cancel",
            entity_type="import_batch",
            entity_id=str(batch_id),
            before_data={"status": "draft"},
            after_data={"status": "cancelled"},
            reversible=False,
        )
    )
    await session.commit()
    return True


async def resolve_import_row(
    session: AsyncSession,
    row_id: int,
    pharmacy_id: int,
    admin_id: int,
) -> ImportRow:
    row = await session.get(ImportRow, row_id)
    pharmacy = await get_pharmacy(session, pharmacy_id)
    if row is None or pharmacy is None:
        raise ValueError("السطر أو الصيدلية غير موجود")
    row.matched_pharmacy_id = pharmacy_id
    row.confidence = 100.0
    row.errors = [error for error in row.errors if "صيدلية" not in error and "مطابقة" not in error]
    row.status = "ready" if not row.errors else "needs_review"
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="import_row_match",
            entity_type="import_row",
            entity_id=str(row.id),
            after_data={"pharmacy_id": pharmacy_id},
            reversible=False,
        )
    )
    await session.commit()
    await session.refresh(row)
    return row


async def count_open_import_errors(session: AsyncSession) -> int:
    batches = await list_draft_batches(session, limit=100)
    return sum(1 for batch in batches for row in batch.rows if row.errors)


async def get_last_reversible_audit(session: AsyncSession, admin_id: int) -> AuditLog | None:
    return await session.scalar(
        select(AuditLog)
        .where(
            AuditLog.admin_id == admin_id,
            AuditLog.reversible.is_(True),
            AuditLog.reversed_at.is_(None),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )


async def undo_last_action(session: AsyncSession, admin_id: int) -> str:
    audit = await get_last_reversible_audit(session, admin_id)
    if audit is None:
        raise ValueError("لا توجد عملية قابلة للتراجع")

    if audit.action == "pharmacy_create" and audit.entity_id:
        pharmacy = await session.get(Pharmacy, int(audit.entity_id))
        if pharmacy:
            pharmacy.deleted_at = utcnow()
            pharmacy.status = "deleted"
    elif audit.action == "pharmacy_delete" and audit.entity_id:
        pharmacy = await session.get(Pharmacy, int(audit.entity_id))
        if pharmacy and audit.before_data:
            pharmacy.deleted_at = None
            _apply_pharmacy_snapshot(pharmacy, audit.before_data)
    elif audit.action == "pharmacy_update" and audit.entity_id:
        pharmacy = await session.get(Pharmacy, int(audit.entity_id))
        if pharmacy and audit.before_data:
            _apply_pharmacy_snapshot(pharmacy, audit.before_data)
            pharmacy.aliases.clear()
            for alias in audit.before_data.get("aliases", []):
                pharmacy.aliases.append(
                    PharmacyAlias(alias=alias, normalized_alias=normalize_text(alias))
                )
    elif audit.action == "shift_create" and audit.entity_id:
        shift = await session.get(Shift, int(audit.entity_id))
        if shift:
            shift.active = False
    elif audit.action == "shift_delete" and audit.entity_id:
        shift = await session.get(Shift, int(audit.entity_id))
        if shift:
            shift.active = True
    elif audit.action == "shift_update" and audit.entity_id and audit.before_data:
        shift = await session.get(Shift, int(audit.entity_id))
        if shift:
            _apply_shift_snapshot(shift, audit.before_data)
    elif audit.action == "shift_bulk_delete" and audit.before_data:
        for snapshot in audit.before_data.get("shifts", []):
            shift = await session.get(Shift, int(snapshot["id"]))
            if shift:
                shift.active = True
    elif audit.action == "batch_publish" and audit.entity_id:
        batch_id = int(audit.entity_id)
        await session.execute(update(Shift).where(Shift.import_batch_id == batch_id).values(active=False))
        batch = await session.get(ImportBatch, batch_id)
        if batch:
            batch.status = "draft"
            batch.published_at = None
        if audit.before_data:
            removed_ids = audit.before_data.get("removed_shift_ids", [])
            if removed_ids:
                await session.execute(
                    update(Shift).where(Shift.id.in_(removed_ids)).values(active=True)
                )
    else:
        raise ValueError("هذه العملية لم يعد ممكناً التراجع عنها")

    audit.reversed_at = utcnow()
    await session.commit()
    return audit.action


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    setting = await session.get(BotSetting, key)
    return default if setting is None else setting.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    setting = await session.get(BotSetting, key)
    if value is None:
        if setting is not None:
            await session.delete(setting)
    elif setting is None:
        session.add(BotSetting(key=key, value=value))
    else:
        setting.value = value
    await session.commit()


async def statistics(session: AsyncSession) -> dict[str, Any]:
    return {
        "users": await user_count(session),
        "pharmacies": await pharmacy_count(session),
        "shifts": await shift_count(session),
        "drafts": len(await list_draft_batches(session, limit=100)),
        "errors": await count_open_import_errors(session),
        "admins": len(await list_admins(session)),
        "latest_shift_end": await latest_shift_end(session),
    }


def summarize_import_rows(rows: Iterable[ImportRow]) -> dict[str, int]:
    rows = list(rows)
    return {
        "total": len(rows),
        "ready": sum(1 for row in rows if not row.errors and row.matched_pharmacy_id),
        "needs_review": sum(1 for row in rows if row.errors),
        "unmatched": sum(1 for row in rows if not row.matched_pharmacy_id),
    }


def serialize_pharmacy(pharmacy: Pharmacy) -> dict[str, Any]:
    return {
        "id": pharmacy.id,
        "name": pharmacy.name,
        "normalized_name": pharmacy.normalized_name,
        "address": pharmacy.address,
        "status": pharmacy.status,
        "notes": pharmacy.notes,
        "aliases": [alias.alias for alias in pharmacy.aliases],
    }


def serialize_shift(shift: Shift) -> dict[str, Any]:
    return {
        "id": shift.id,
        "pharmacy_id": shift.pharmacy_id,
        "start_at": shift.start_at.isoformat(),
        "end_at": shift.end_at.isoformat(),
        "active": shift.active,
        "import_batch_id": shift.import_batch_id,
        "created_by": shift.created_by,
    }


def _apply_pharmacy_snapshot(pharmacy: Pharmacy, data: dict[str, Any]) -> None:
    pharmacy.name = data["name"]
    pharmacy.normalized_name = data["normalized_name"]
    pharmacy.address = data["address"]
    pharmacy.status = data["status"]
    pharmacy.notes = data.get("notes")


def _apply_shift_snapshot(shift: Shift, data: dict[str, Any]) -> None:
    shift.pharmacy_id = int(data["pharmacy_id"])
    shift.start_at = datetime.fromisoformat(data["start_at"])
    shift.end_at = datetime.fromisoformat(data["end_at"])
    shift.active = bool(data.get("active", True))
    shift.import_batch_id = data.get("import_batch_id")
    shift.created_by = data.get("created_by")

async def latest_published_at(session: AsyncSession) -> datetime | None:
    return await session.scalar(
        select(func.max(ImportBatch.published_at)).where(ImportBatch.status == "published")
    )

async def rematch_import_batch(session: AsyncSession, batch_id: int, admin_id: int) -> int:
    batch = await get_import_batch(session, batch_id)
    if batch is None or batch.status != "draft":
        raise ValueError("المسودة غير موجودة")
    matched_count = 0
    for row in batch.rows:
        if row.matched_pharmacy_id:
            continue
        pharmacy, confidence = await match_pharmacy(session, row.raw_pharmacy_name)
        if pharmacy:
            row.matched_pharmacy_id = pharmacy.id
            row.confidence = confidence
            row.errors = [
                error
                for error in row.errors
                if "الصيدلية غير مطابقة" not in error and "مطابقة" not in error
            ]
            row.status = "ready" if not row.errors else "needs_review"
            matched_count += 1
    batch.summary = summarize_import_rows(batch.rows)
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="batch_rematch",
            entity_type="import_batch",
            entity_id=str(batch_id),
            after_data={"matched": matched_count},
            reversible=False,
        )
    )
    await session.commit()
    return matched_count

async def list_all_shifts(session: AsyncSession, limit: int = 10000) -> list[Shift]:
    result = await session.scalars(
        select(Shift)
        .options(selectinload(Shift.pharmacy))
        .join(Shift.pharmacy)
        .where(Shift.active.is_(True), Pharmacy.deleted_at.is_(None))
        .order_by(Shift.start_at, Pharmacy.name)
        .limit(limit)
    )
    return list(result)


async def record_usage_event(
    session: AsyncSession,
    user_id: int,
    event: str,
    event_data: dict[str, Any] | None = None,
) -> None:
    session.add(UsageEvent(user_id=user_id, event=event, event_data=event_data or {}))
    await session.commit()


async def usage_statistics(session: AsyncSession) -> dict[str, Any]:
    searches = int(
        await session.scalar(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.event == "user_search")
        )
        or 0
    )
    starts = int(
        await session.scalar(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.event == "start")
        )
        or 0
    )
    button_rows = await session.execute(
        select(UsageEvent.event, func.count(UsageEvent.id))
        .where(UsageEvent.event.in_(["view_now", "view_today", "view_tomorrow", "user_search"]))
        .group_by(UsageEvent.event)
        .order_by(func.count(UsageEvent.id).desc())
    )
    popular = [(str(name), int(count)) for name, count in button_rows.all()]
    return {"starts": starts, "searches": searches, "popular_actions": popular}

async def schedule_alert_recipients(session: AsyncSession) -> list[int]:
    result = await session.scalars(
        select(Admin.telegram_id).where(
            Admin.active.is_(True),
            Admin.role.in_(["owner", "admin"]),
        )
    )
    return list(result)
