from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class AntiSpamMiddleware(BaseMiddleware):
    """Small in-memory anti-flood guard for messages and inline-button clicks."""

    def __init__(
        self,
        *,
        message_cooldown: float = 1.0,
        callback_cooldown: float = 0.65,
        warning_cooldown: float = 5.0,
    ) -> None:
        self.message_cooldown = message_cooldown
        self.callback_cooldown = callback_cooldown
        self.warning_cooldown = warning_cooldown
        self._last_event: dict[tuple[int, str], float] = {}
        self._last_warning: dict[tuple[int, str], float] = {}
        self._active: set[tuple[int, str]] = set()
        self._lock = asyncio.Lock()

    async def _try_enter(
        self,
        user_id: int,
        event_kind: str,
        *,
        now: float | None = None,
    ) -> bool:
        current = monotonic() if now is None else now
        key = (user_id, event_kind)
        cooldown = (
            self.callback_cooldown
            if event_kind == "callback"
            else self.message_cooldown
        )

        async with self._lock:
            previous = self._last_event.get(key, float("-inf"))
            if key in self._active or current - previous < cooldown:
                return False
            self._last_event[key] = current
            self._active.add(key)

            if len(self._last_event) > 10_000:
                stale_before = current - 300
                self._last_event = {
                    stored_key: stored_at
                    for stored_key, stored_at in self._last_event.items()
                    if stored_at >= stale_before
                }
                self._last_warning = {
                    stored_key: stored_at
                    for stored_key, stored_at in self._last_warning.items()
                    if stored_at >= stale_before
                }
            return True

    async def _leave(self, user_id: int, event_kind: str) -> None:
        async with self._lock:
            self._active.discard((user_id, event_kind))

    async def _warn(self, event: TelegramObject, user_id: int, event_kind: str) -> None:
        current = monotonic()
        key = (user_id, event_kind)
        async with self._lock:
            previous = self._last_warning.get(key, float("-inf"))
            if current - previous < self.warning_cooldown:
                should_warn = False
            else:
                self._last_warning[key] = current
                should_warn = True

        if not should_warn:
            return

        try:
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ انتظر لحظة قبل الضغط مرة ثانية.")
            elif isinstance(event, Message):
                await event.answer("⏳ انتظر شوي قبل الإرسال مرة ثانية.")
        except Exception:
            # A failed warning must never interrupt normal bot operation.
            return

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        event_kind = "callback" if isinstance(event, CallbackQuery) else "message"
        allowed = await self._try_enter(user.id, event_kind)
        if not allowed:
            await self._warn(event, user.id, event_kind)
            return None

        try:
            return await handler(event, data)
        finally:
            await self._leave(user.id, event_kind)
