from __future__ import annotations

from types import SimpleNamespace

from app.middlewares import _message_command, _message_kind


def test_document_without_text_does_not_crash_command_parser() -> None:
    message = SimpleNamespace(
        text=None,
        photo=None,
        document=object(),
        location=None,
        contact=None,
    )

    assert _message_command(message) == ""
    assert _message_kind(message) == "document"


def test_start_command_is_still_detected() -> None:
    message = SimpleNamespace(text="/start payload")
    assert _message_command(message) == "/start"
