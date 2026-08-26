from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.models import Base


class Database:
    def __init__(self, settings: Settings):
        connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
        engine_options: dict[str, Any] = {
            "pool_pre_ping": True,
            "connect_args": connect_args,
        }
        if settings.run_mode == "webhook" and settings.database_url.startswith("postgresql"):
            # A persistent PostgreSQL pool emits background network traffic and
            # prevents Railway Serverless from considering the service idle.
            engine_options["poolclass"] = NullPool
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            **engine_options,
        )
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

        if settings.database_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    async def init(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session
