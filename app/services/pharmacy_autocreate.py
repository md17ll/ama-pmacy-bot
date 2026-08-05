from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.utils import normalize_text


def compact_pharmacy_key(value: str) -> str:
    """Normalize a pharmacy name and ignore accidental spacing differences."""
    return normalize_text(value).replace(" ", "")


def group_pharmacy_names(names: Iterable[str]) -> list[tuple[str, list[str]]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for raw_name in names:
        name = " ".join((raw_name or "").split()).strip()
        key = compact_pharmacy_key(name)
        if name and key:
            groups[key].add(name)

    grouped: list[tuple[str, list[str]]] = []
    for key, variants in groups.items():
        # Prefer the spelling containing useful spaces, such as "محمد حسو"
        # instead of "محمدحسو".
        ordered = sorted(
            variants,
            key=lambda value: (-value.count(" "), -len(value), value),
        )
        grouped.append((key, ordered))
    grouped.sort(key=lambda item: item[1][0])
    return grouped


async def create_missing_pharmacy_names(
    session: AsyncSession,
    names: Iterable[str],
    *,
    admin_id: int,
    source_note: str,
) -> tuple[int, int]:
    """Create missing pharmacy records with blank addresses.

    Returns (created_count, existing_count). Names differing only by spaces are
    treated as one pharmacy and stored as aliases.
    """
    existing = await repositories.list_pharmacies(
        session,
        include_inactive=True,
        limit=5000,
    )
    existing_keys: set[str] = set()
    for pharmacy in existing:
        existing_keys.add(compact_pharmacy_key(pharmacy.name))
        existing_keys.update(
            compact_pharmacy_key(alias.alias)
            for alias in pharmacy.aliases
            if compact_pharmacy_key(alias.alias)
        )

    created = 0
    already_existing = 0
    for key, variants in group_pharmacy_names(names):
        if key in existing_keys:
            already_existing += 1
            continue

        canonical = variants[0]
        aliases = variants[1:]
        try:
            await repositories.create_pharmacy(
                session,
                name=canonical,
                address="",
                aliases=aliases,
                status="active",
                notes=source_note,
                admin_id=admin_id,
            )
        except ValueError:
            # A concurrent import or a spelling already present in the database
            # should not abort the whole batch.
            already_existing += 1
            continue

        created += 1
        existing_keys.add(key)

    return created, already_existing
