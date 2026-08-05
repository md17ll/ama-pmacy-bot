from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "app" / "handlers"
AUTH_HELPERS = {"require_admin", "require_writer", "require_owner", "_render_admin_home"}


def _is_admin_callback_decorator(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "callback_query"
        and isinstance(func.value, ast.Name)
        and func.value.id == "router"
    ):
        return False
    rendered = ast.unparse(node.args[0])
    return '"a:' in rendered or "'a:" in rendered or "cb.ADMIN_" in rendered


def _called_names(statements: list[ast.stmt]) -> set[str]:
    result: set[str] = set()
    for statement in statements:
        for child in ast.walk(statement):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    result.add(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    result.add(child.func.attr)
    return result


def test_admin_callback_handlers_have_authorization_guard() -> None:
    failures: list[str] = []
    for path in HANDLERS.glob("*.py"):
        if path.name in {"user.py", "common.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not any(_is_admin_callback_decorator(item) for item in node.decorator_list):
                continue
            calls = _called_names(node.body[:6])
            if not (calls & AUTH_HELPERS):
                failures.append(f"{path.name}:{node.name}")
    assert not failures, f"Admin callback handlers without auth guard: {failures}"


def test_no_plaintext_secrets_are_committed() -> None:
    telegram_token = re.compile(r"(?<![A-Za-z0-9_])\d{8,12}:[A-Za-z0-9_-]{30,}")
    google_key = re.compile(r"AIza[0-9A-Za-z_-]{30,}")
    postgres_password = re.compile(
        r"postgres(?:ql)?(?:\+asyncpg)?://[^/\s:@]+:[^/\s@]+@",
        re.IGNORECASE,
    )
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".bootstrap" in path.parts:
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".xlsx", ".zip", ".xz"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if telegram_token.search(text) or google_key.search(text) or postgres_password.search(text):
            findings.append(str(path.relative_to(ROOT)))
    assert not findings, f"Possible plaintext secrets: {findings}"
