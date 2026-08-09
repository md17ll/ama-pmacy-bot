from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.handlers.smart_schedule_edit_confirm import _prepare_manual_metadata


def _row(*, manual: bool = False):
    data = {"period": "day", "locked": False}
    if manual:
        data.update(
            {
                "manual_override": True,
                "generated_pharmacy_id": 7,
                "generated_pharmacy_name": "صيدلية المولّد",
                "locked": True,
            }
        )
    return SimpleNamespace(
        raw_data=data,
        matched_pharmacy_id=7 if not manual else 11,
        matched_pharmacy=SimpleNamespace(
            name="صيدلية المولّد" if not manual else "صيدلية تعديل أول"
        ),
        raw_pharmacy_name="صيدلية المولّد" if not manual else "صيدلية تعديل أول",
    )


def test_first_confirmed_edit_preserves_generator_choice_for_revert() -> None:
    data = _prepare_manual_metadata(_row())
    assert data["manual_override"] is True
    assert data["locked"] is True
    assert data["generated_pharmacy_id"] == 7
    assert data["generated_pharmacy_name"] == "صيدلية المولّد"


def test_second_confirmed_edit_does_not_replace_generator_baseline() -> None:
    data = _prepare_manual_metadata(_row(manual=True))
    assert data["generated_pharmacy_id"] == 7
    assert data["generated_pharmacy_name"] == "صيدلية المولّد"


def test_confirmation_handler_is_registered_before_legacy_immediate_pick_handler() -> None:
    init_source = Path("app/handlers/__init__.py").read_text(encoding="utf-8")
    confirm_position = init_source.index("smart_schedule_edit_confirm")
    legacy_position = init_source.index("from . import smart_schedules")
    assert confirm_position < legacy_position


def test_pick_requires_savepick_confirmation_button_in_source() -> None:
    source = Path("app/handlers/smart_schedule_edit_confirm.py").read_text(encoding="utf-8")
    assert '@router.callback_query(F.data.startswith("a:smart:pick:"))' in source
    assert '"✅ حفظ التعديل"' in source
    assert 'f"a:smart:savepick:{batch_id}:{row_id}:{pharmacy_id}"' in source
    assert '"❌ إلغاء"' in source
