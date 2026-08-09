from __future__ import annotations

from types import SimpleNamespace

from app.handlers.smart_schedule_guard import _batch_id_from_callback
from app.handlers.smart_schedule_template_editor import _manual_choice_data, _manual_metadata


def _row():
    return SimpleNamespace(
        raw_data={"period": "day", "locked": False},
        matched_pharmacy_id=7,
        matched_pharmacy=SimpleNamespace(name="صيدلية الأصل"),
        raw_pharmacy_name="صيدلية الأصل",
    )


def test_manual_edit_keeps_original_generator_choice_for_restore() -> None:
    row = _row()
    first = _manual_choice_data(row)
    assert first["manual_override"] is True
    assert first["locked"] is True
    assert first["generated_pharmacy_id"] == 7
    assert first["generated_pharmacy_name"] == "صيدلية الأصل"
    assert first["generated_locked"] is False

    # Editing the same row again must not replace the original generator baseline.
    row.raw_data = first
    row.matched_pharmacy_id = 12
    row.matched_pharmacy = SimpleNamespace(name="صيدلية تعديل أول")
    second = _manual_choice_data(row)
    assert second["generated_pharmacy_id"] == 7
    assert second["generated_pharmacy_name"] == "صيدلية الأصل"


def test_reroll_metadata_only_preserves_confirmed_locked_manual_choice() -> None:
    row = _row()
    row.raw_data = _manual_choice_data(row)
    metadata = _manual_metadata(row)
    assert metadata is not None
    assert metadata["manual_override"] is True
    assert metadata["generated_pharmacy_id"] == 7

    row.raw_data = {**row.raw_data, "locked": False}
    assert _manual_metadata(row) is None


def test_guard_validates_new_save_and_restore_callbacks() -> None:
    assert _batch_id_from_callback("a:smart:savepick:15:22:4") == (True, 15)
    assert _batch_id_from_callback("a:smart:restore:15:22") == (True, 15)
    assert _batch_id_from_callback("a:smart:savepick:not-an-id:22:4") == (True, None)
