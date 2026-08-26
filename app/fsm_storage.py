from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from app.db import Database
from app.models import FSMRecord


class DatabaseFSMStorage(BaseStorage):
    """Store short-lived Telegram conversation state in the existing database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _key(key: StorageKey) -> str:
        return json.dumps(
            [
                key.bot_id,
                key.chat_id,
                key.user_id,
                key.thread_id,
                key.business_connection_id,
                key.destiny,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        storage_key = self._key(key)
        async with self.db.session_factory() as session:
            record = await session.get(FSMRecord, storage_key)
            if record is None:
                if value is None:
                    return
                session.add(FSMRecord(storage_key=storage_key, state=value, data={}))
            else:
                record.state = value
                if value is None and not record.data:
                    await session.delete(record)
            await session.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with self.db.session_factory() as session:
            record = await session.get(FSMRecord, self._key(key))
            return record.state if record is not None else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            raise DataNotDictLikeError(
                f"Data must be a dict or dict-like object, got {type(data).__name__}"
            )
        value = data.copy()
        storage_key = self._key(key)
        async with self.db.session_factory() as session:
            record = await session.get(FSMRecord, storage_key)
            if record is None:
                if not value:
                    return
                session.add(FSMRecord(storage_key=storage_key, state=None, data=value))
            else:
                record.data = value
                if not value and record.state is None:
                    await session.delete(record)
            await session.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self.db.session_factory() as session:
            record = await session.get(FSMRecord, self._key(key))
            return dict(record.data or {}) if record is not None else {}

    async def close(self) -> None:
        # The Database lifecycle is owned by the runner.
        return None
