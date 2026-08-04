from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.enums import ButtonStyle

from app import callbacks as cb
from app.middlewares import AntiSpamMiddleware
from app.public_keyboards import user_home, user_results


def _all_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_home_button_colors() -> None:
    markup = user_home()
    buttons = {button.text: button for button in _all_buttons(markup)}

    assert buttons["📅 صيدليات اليوم"].style == ButtonStyle.SUCCESS
    assert buttons["⏭ صيدليات غداً"].style == ButtonStyle.SUCCESS
    assert buttons["🔄 تحديث الوقت"].style == ButtonStyle.DANGER


def test_results_only_show_pharmacies_and_contextual_refresh() -> None:
    pharmacy_one = SimpleNamespace(id=1, name="صيدلية الشفاء")
    pharmacy_two = SimpleNamespace(id=2, name="صيدلية الأمل")
    shifts = [
        SimpleNamespace(pharmacy=pharmacy_one),
        SimpleNamespace(pharmacy=pharmacy_one),
        SimpleNamespace(pharmacy=pharmacy_two),
    ]

    markup = user_results(shifts, refresh_callback=cb.USER_TOMORROW)
    buttons = _all_buttons(markup)

    assert [button.text for button in buttons] == [
        "💊 صيدلية الشفاء",
        "💊 صيدلية الأمل",
        "🔄 تحديث الوقت",
    ]
    assert buttons[-1].callback_data == cb.USER_TOMORROW
    assert buttons[-1].style == ButtonStyle.DANGER
    assert all(button.callback_data != cb.USER_HOME for button in buttons)


def test_results_without_shifts_still_have_refresh() -> None:
    markup = user_results([], refresh_callback=cb.USER_NOW)
    buttons = _all_buttons(markup)

    assert len(buttons) == 1
    assert buttons[0].text == "🔄 تحديث الوقت"
    assert buttons[0].callback_data == cb.USER_NOW


def test_antispam_cooldown_and_release() -> None:
    async def scenario() -> None:
        middleware = AntiSpamMiddleware(callback_cooldown=0.65)

        assert await middleware._try_enter(100, "callback", now=10.0)
        assert not await middleware._try_enter(100, "callback", now=10.1)
        await middleware._leave(100, "callback")
        assert not await middleware._try_enter(100, "callback", now=10.2)
        assert await middleware._try_enter(100, "callback", now=10.8)
        await middleware._leave(100, "callback")

    asyncio.run(scenario())
