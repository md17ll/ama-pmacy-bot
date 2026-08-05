from __future__ import annotations

from types import SimpleNamespace

from app import keyboards
from app.services.pharmacy_autocreate import compact_pharmacy_key, group_pharmacy_names


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_spacing_variants_are_grouped_as_one_pharmacy() -> None:
    groups = group_pharmacy_names(
        [
            "محمدحسو",
            "محمد حسو",
            "صيدلية محمد حسو",
            "نور",
            "نور",
        ]
    )
    by_key = {key: variants for key, variants in groups}

    assert compact_pharmacy_key("محمدحسو") == compact_pharmacy_key("محمد حسو")
    assert compact_pharmacy_key("صيدلية محمد حسو") == compact_pharmacy_key("محمد حسو")
    assert by_key[compact_pharmacy_key("محمد حسو")][0] in {
        "صيدلية محمد حسو",
        "محمد حسو",
    }
    assert by_key[compact_pharmacy_key("نور")] == ["نور"]


def test_pharmacy_detail_can_edit_every_field() -> None:
    markup = keyboards.pharmacy_detail(17, "active")
    callbacks = {button.callback_data for button in _buttons(markup)}

    assert "a:p:edit:17:name" in callbacks
    assert "a:p:edit:17:address" in callbacks
    assert "a:p:edit:17:aliases" in callbacks
    assert "a:p:edit:17:notes" in callbacks
    assert "a:p:status_menu:17" in callbacks
    assert "a:p:delete_ask:17" in callbacks


def test_pharmacy_status_menu_has_all_supported_states() -> None:
    markup = keyboards.pharmacy_status(17, "temporarily_closed")
    callbacks = {button.callback_data for button in _buttons(markup)}

    assert "a:p:status:17:active" in callbacks
    assert "a:p:status:17:temporarily_closed" in callbacks
    assert "a:p:status:17:inactive" in callbacks
    assert "a:p:view:17" in callbacks


def test_incomplete_pharmacy_is_marked_in_list() -> None:
    pharmacy = SimpleNamespace(id=9, name="نور", address="")
    markup = keyboards.pharmacy_list([pharmacy])

    assert _buttons(markup)[0].text == "⚠️ نور"


def test_draft_has_automatic_name_import_button() -> None:
    markup = keyboards.draft_detail(4)
    callbacks = {button.callback_data for button in _buttons(markup)}

    assert "a:d:auto_pharmacies:4" in callbacks
    assert "a:d:missing:4" in callbacks
