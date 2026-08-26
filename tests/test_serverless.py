from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from aiogram.fsm.storage.base import StorageKey
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.db import Database
from app.fsm_storage import DatabaseFSMStorage
from app.models import FSMRecord
from app.runner import build_dispatcher


ROOT = Path(__file__).resolve().parents[1]


def _settings(database_url: str, *, run_mode: str = "webhook") -> Settings:
    return Settings(
        bot_token="test-token",
        gemini_api_key=None,
        database_url=database_url,
        owner_ids=(1,),
        timezone_name="Asia/Damascus",
        run_mode=run_mode,
        webhook_base_url="https://example.test" if run_mode == "webhook" else None,
        webhook_path="/telegram/webhook",
        webhook_secret="test-secret",
        port=8080,
        gemini_model="test-model",
        log_level="INFO",
    )


def test_database_fsm_storage_round_trip_and_clear(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'fsm.db'}"))
        await db.init()
        storage = DatabaseFSMStorage(db)
        key = StorageKey(bot_id=1, chat_id=2, user_id=3)
        try:
            assert await storage.get_state(key) is None
            assert await storage.get_data(key) == {}

            await storage.set_state(key, "AdminImportState:waiting_image")
            await storage.set_data(key, {"menu_message_id": 99})
            assert await storage.get_state(key) == "AdminImportState:waiting_image"
            assert await storage.get_data(key) == {"menu_message_id": 99}

            updated = await storage.update_data(key, {"chat_id": 2})
            assert updated == {"menu_message_id": 99, "chat_id": 2}

            await storage.set_state(key, None)
            await storage.set_data(key, {})
            assert await storage.get_state(key) is None
            assert await storage.get_data(key) == {}
            async with db.session_factory() as session:
                assert await session.get(FSMRecord, storage._key(key)) is None
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_dispatcher_uses_persistent_storage(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'dispatcher.db'}"))
        try:
            dispatcher = build_dispatcher(db)
            assert isinstance(dispatcher.storage, DatabaseFSMStorage)
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_webhook_postgres_does_not_keep_an_idle_pool() -> None:
    db = Database(_settings("postgresql+asyncpg://localhost/amuda"))
    assert isinstance(db.engine.sync_engine.pool, NullPool)


def test_office_libraries_are_lazy_loaded() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.runner; "
                "print(int('openpyxl' in sys.modules), int('docx' in sys.modules))"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "0 0"
