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


def test_generator_refuses_to_replace_a_friday_fixed_by_photo(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'photo-overlap.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                await repositories.create_pharmacy(session, name="صيدلية أ", address="عامودا", admin_id=1)
                await repositories.create_pharmacy(session, name="صيدلية ب", address="عامودا", admin_id=1)
                with pytest.raises(ValueError, match="جمعات مثبتة في الصور"):
                    await smart.generate_import_rows(
                        session,
                        start_date=date(2026, 8, 10),
                        end_date=date(2026, 8, 20),
                        timezone=TZ,
                        times=ShiftTimes(),
                    )
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_generator_refuses_period_crossing_august_cycle_reset(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'cycle-boundary.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                await repositories.create_pharmacy(session, name="صيدلية أ", address="عامودا", admin_id=1)
                await repositories.create_pharmacy(session, name="صيدلية ب", address="عامودا", admin_id=1)
                with pytest.raises(ValueError, match="01/08"):
                    await smart.generate_import_rows(
                        session,
                        start_date=date(2027, 7, 30),
                        end_date=date(2027, 8, 2),
                        timezone=TZ,
                        times=ShiftTimes(),
                    )
        finally:
            await db.dispose()

    asyncio.run(scenario())
