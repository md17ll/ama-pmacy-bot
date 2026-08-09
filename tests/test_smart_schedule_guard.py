from __future__ import annotations

import pytest

from app.handlers.smart_schedule_guard import _batch_id_from_callback


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("a:smart:draft:12", (True, 12)),
        ("a:smart:view:12:0", (True, 12)),
        ("a:smart:choose:12:99:0", (True, 12)),
        ("a:smart:pick:12:99:5", (True, 12)),
        ("a:smart:savepick:12:99:5", (True, 12)),
        ("a:smart:savepick:not-a-number:99:5", (True, None)),
        ("a:smart:publish:12", (True, 12)),
        ("a:smart:delete:12", (True, 12)),
        ("a:smart:advanced:draft:12", (True, 12)),
        ("a:smart:publish:not-a-number", (True, None)),
        ("a:smart:word", (True, None)),
        ("a:smart:new", (False, None)),
        ("a:smart:stats", (False, None)),
        ("a:smart:friday:0", (False, None)),
    ],
)
def test_batch_callback_guard_parsing(callback_data, expected) -> None:
    assert _batch_id_from_callback(callback_data) == expected
