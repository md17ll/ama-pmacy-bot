from __future__ import annotations

import asyncio
from datetime import date
from zoneinfo import ZoneInfo

import pytest

import app.handlers  # noqa: F401 - activates the smart schedule integration patches
from app import repositories
from app.config import Settings
from app.db import Database
from app.services import smart_schedule as smart
from app.services.friday_overrides import (
    build_friday_states,
    clear_friday_override,
    set_friday_override,
    state_source_label,
    unmatched_photo_names,
)
from app.services.shift_schedule_tools import ShiftTimes


TZ = ZoneInfo("Asia/Damascus")


def _settings(database_url: str) -> Settings:
    return Settings(
        bot_token="test-token",
        gemini_api_key=None,
        database_url=database_url,
        owner_ids=(1,),
        timezone_name="Asia/Damascus",
        run_mode="polling",
        webhook_base_url=None,
        webhook_path="/telegram/webhook",
        webhook_secret=None,
        port=8080,
        gemini_model="gemini-test",
        log_level="INFO",
    )


def test_august_photo_entries_start_new_cycle_at_one_of_two(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'august-cycle.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                names = ["عصام", "محمد حسو", "رشاد", "زنار", "صيدلية جديدة"]
                for name in names:
                    await repositories.create_pharmacy(
                        session,
                        name=name,
                        address="عامودا",
                        admin_id=1,
                    )
                active, shifts = await smart._active_pharmacies_and_shifts(session)
                states = await build_friday_states(
                    session,
                    active,
                    shifts,
                    reference_date=date(2026, 8, 18),
                    before_date=date(2026, 8, 18),
                    timezone=TZ,
                )
                by_name = {state.name: state for state in states.values()}
                assert by_name["عصام"].effective_count == 1
                assert by_name["محمد حسو"].effective_count == 1
                assert by_name["رشاد"].effective_count == 1
                assert by_name["زنار"].effective_count == 1
                assert by_name["صيدلية جديدة"].effective_count == 0
                assert date(2026, 2, 20) not in by_name["عصام"].image_dates
                assert date(2026, 8, 7) in by_name["عصام"].image_dates
                assert unmatched_photo_names(
                    active,
                    reference_date=date(2026, 8, 18),
                    before_date=date(2026, 8, 18),
                ) == []
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_unmatched_photo_name_is_reported_instead_of_silently_dropped(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'unmatched.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                for name in ("عصام", "محمد حسو", "رشاد"):
                    await repositories.create_pharmacy(session, name=name, address="عامودا", admin_id=1)
                active, _shifts = await smart._active_pharmacies_and_shifts(session)
                missing = unmatched_photo_names(
                    active,
                    reference_date=date(2026, 8, 18),
                    before_date=date(2026, 8, 18),
                )
                assert missing == ["زنار"]
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_manual_override_can_be_set_and_reverted_to_photo(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'override.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                pharmacy = await repositories.create_pharmacy(
                    session,
                    name="عصام",
                    address="عامودا",
                    admin_id=1,
                )
                active, shifts = await smart._active_pharmacies_and_shifts(session)
                states = await build_friday_states(
                    session,
                    active,
                    shifts,
                    reference_date=date(2026, 8, 18),
                    before_date=date(2026, 8, 18),
                    timezone=TZ,
                )
                assert states[pharmacy.id].effective_count == 1

                await set_friday_override(
                    session,
                    reference_date=date(2026, 8, 18),
                    pharmacy_id=pharmacy.id,
                    count=2,
                    admin_id=1,
                )
                states = await build_friday_states(
                    session,
                    active,
                    shifts,
                    reference_date=date(2026, 8, 18),
                    before_date=date(2026, 8, 18),
                    timezone=TZ,
                )
                assert states[pharmacy.id].effective_count == 2
                assert state_source_label(states[pharmacy.id]) == "تعديل يدوي"

                await clear_friday_override(
                    session,
                    reference_date=date(2026, 8, 18),
                    pharmacy_id=pharmacy.id,
                    admin_id=1,
                )
                states = await build_friday_states(
                    session,
                    active,
                    shifts,
                    reference_date=date(2026, 8, 18),
                    before_date=date(2026, 8, 18),
                    timezone=TZ,
                )
                assert states[pharmacy.id].effective_count == 1
                assert states[pharmacy.id].override_count is None
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_override_cannot_go_below_published_friday_count(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'minimum.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                pharmacy = await repositories.create_pharmacy(
                    session,
                    name="صيدلية اختبار",
                    address="عامودا",
                    admin_id=1,
                )
                with pytest.raises(ValueError, match="جمعة منشورة"):
                    await set_friday_override(
                        session,
                        reference_date=date(2026, 8, 18),
                        pharmacy_id=pharmacy.id,
                        count=0,
                        admin_id=1,
                        minimum_count=1,
                    )
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_manually_added_active_pharmacy_is_used_by_smart_generator(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'manual-pharmacy.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                first = await repositories.create_pharmacy(
                    session,
                    name="صيدلية جديدة أ",
                    address="عامودا",
                    admin_id=1,
                )
                second = await repositories.create_pharmacy(
                    session,
                    name="صيدلية جديدة ب",
                    address="عامودا",
                    admin_id=1,
                )
                prepared, analysis = await smart.generate_import_rows(
                    session,
                    start_date=date(2026, 8, 21),
                    end_date=date(2026, 8, 21),
                    timezone=TZ,
                    times=ShiftTimes(),
                )
                assert {row["matched_pharmacy_id"] for row in prepared} == {first.id, second.id}
                assert analysis.total_assignments == 2
                assert analysis.friday_over_limit == 0
        finally:
            await db.dispose()

    asyncio.run(scenario())
