from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.filters import StateFilter

from app import callbacks as cb
from app.handlers import user
from app.states import UserSearchState


def test_unhandled_messages_receive_start_guidance() -> None:
    async def scenario():
        message = SimpleNamespace(answer=AsyncMock())
        await user.unknown_message_handler(message)
        message.answer.assert_awaited_once()
        response = message.answer.await_args.args[0]
        assert "صيدليات عامودا" in response
        assert "<code>/start</code>" in response

    asyncio.run(scenario())


def test_fallback_is_last_and_only_applies_when_not_in_fsm() -> None:
    handler = user.router.message.handlers[-1]
    assert handler.callback is user.unknown_message_handler
    state_filters = [
        item.callback for item in handler.filters
        if isinstance(item.callback, StateFilter)
    ]
    assert len(state_filters) == 1
    state_filter = state_filters[0]
    assert asyncio.run(state_filter(None, raw_state=None))
    assert not asyncio.run(
        state_filter(None, raw_state=UserSearchState.waiting_query.state)
    )


def test_existing_public_routes_and_search_are_preserved() -> None:
    callbacks = [
        handler.callback for handler in user.router.callback_query.handlers
    ]
    assert user.current_shifts_handler in callbacks  # Old already-sent buttons still work
    assert user.today_shifts_handler in callbacks
    assert user.tomorrow_shifts_handler in callbacks
    assert user.search_prompt_handler in callbacks

    message_handlers = [
        handler.callback for handler in user.router.message.handlers
    ]
    assert user.start_handler in message_handlers
    assert user.search_result_handler in message_handlers
    assert message_handlers.index(user.search_result_handler) < message_handlers.index(
        user.unknown_message_handler
    )
