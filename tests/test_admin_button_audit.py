from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import InlineKeyboardMarkup

from app import callbacks as cb, keyboards, public_keyboards


HANDLERS_DIR = Path("app/handlers")


def _resolve_callback(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "cb"
        and hasattr(cb, node.attr)
    ):
        value = getattr(cb, node.attr)
        return value if isinstance(value, str) else None
    return None


def _is_f_data(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "data"
        and isinstance(node.value, ast.Name)
        and node.value.id == "F"
    )


def _extract_handler_matchers() -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    prefixes: set[str] = set()

    for path in HANDLERS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                func = decorator.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "callback_query"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "router"
                ):
                    continue

                filter_node = decorator.args[0]
                if (
                    isinstance(filter_node, ast.Compare)
                    and _is_f_data(filter_node.left)
                    and len(filter_node.ops) == 1
                    and isinstance(filter_node.ops[0], ast.Eq)
                    and len(filter_node.comparators) == 1
                ):
                    value = _resolve_callback(filter_node.comparators[0])
                    if value:
                        exact.add(value)
                    continue

                if isinstance(filter_node, ast.Call) and isinstance(filter_node.func, ast.Attribute):
                    owner = filter_node.func.value
                    method = filter_node.func.attr
                    if not _is_f_data(owner) or not filter_node.args:
                        continue
                    if method == "startswith":
                        value = _resolve_callback(filter_node.args[0])
                        if value:
                            prefixes.add(value)
                    elif method == "in_" and isinstance(
                        filter_node.args[0], (ast.Set, ast.List, ast.Tuple)
                    ):
                        for item in filter_node.args[0].elts:
                            value = _resolve_callback(item)
                            if value:
                                exact.add(value)

    return exact, prefixes


def _collect_markup_callbacks(markup: InlineKeyboardMarkup) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def _all_constructed_buttons() -> set[str]:
    admin = SimpleNamespace(
        telegram_id=200,
        role="admin",
        active=True,
        entry_notifications=True,
    )
    markups = [
        keyboards.admin_home(),
        keyboards.admin_import(),
        keyboards.admin_shifts(),
        keyboards.shift_list([]),
        keyboards.shift_detail(1),
        keyboards.confirm_shift_delete(1),
        keyboards.admin_pharmacies(),
        keyboards.pharmacy_list([]),
        keyboards.pharmacy_detail(1, "active"),
        keyboards.pharmacy_detail(1, "temporarily_closed"),
        keyboards.confirm_pharmacy_delete(1),
        keyboards.drafts([]),
        keyboards.draft_detail(1),
        keyboards.confirm_publish(1, "add"),
        keyboards.confirm_publish(1, "replace"),
        keyboards.confirm_cancel_batch(1),
        keyboards.exports(),
        keyboards.admins([admin], owner=True),
        keyboards.admin_list([admin], owner=True),
        keyboards.confirm_admin_remove(200),
        keyboards.notifications(True),
        keyboards.notifications(False),
        keyboards.simple_back(cb.ADMIN_HOME),
        keyboards.back_user(),
        public_keyboards.user_home(is_admin=True),
        public_keyboards.user_results([], refresh_callback=cb.USER_NOW),
    ]
    callbacks: set[str] = set()
    for markup in markups:
        callbacks.update(_collect_markup_callbacks(markup))
    return callbacks


def _literal_buttons_inside_handlers() -> set[str]:
    values: set[str] = set()
    for path in HANDLERS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "button"
                and isinstance(func.value, ast.Name)
                and func.value.id == "keyboards"
            ):
                continue
            value = _resolve_callback(node.args[1])
            if value:
                values.add(value)
    return values


def test_every_admin_button_has_a_callback_handler() -> None:
    exact, prefixes = _extract_handler_matchers()
    button_callbacks = _all_constructed_buttons() | _literal_buttons_inside_handlers()

    missing = sorted(
        value
        for value in button_callbacks
        if value not in exact and not any(value.startswith(prefix) for prefix in prefixes)
    )

    assert not missing, f"Buttons without callback handlers: {missing}"


def test_all_declared_static_callbacks_are_handled() -> None:
    exact, prefixes = _extract_handler_matchers()
    missing = sorted(
        value
        for value in cb.ALL_STATIC_CALLBACKS
        if value not in exact and not any(value.startswith(prefix) for prefix in prefixes)
    )
    assert not missing, f"Declared callbacks without handlers: {missing}"
