from __future__ import annotations

import ast
from pathlib import Path


def test_admin_preview_uses_current_shifts_not_last_publish_timestamp() -> None:
    path = Path("app/handlers/admin.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    preview = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "admin_preview"
    )
    source = ast.get_source_segment(path.read_text(encoding="utf-8"), preview) or ""
    assert "repositories.current_shifts(session, now)" in source
    assert "texts.user_home_text(now, settings.timezone, current_shifts)" in source
    assert "latest_published_at" not in source
