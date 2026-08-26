from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.handlers.smart_schedule_template_ui_guard import _is_template_draft


def _markup(*texts: str):
    return SimpleNamespace(
        inline_keyboard=[[SimpleNamespace(text=text) for text in texts]]
    )


def test_approved_template_draft_is_detected() -> None:
    text = "⚙️ لوحة الإدارة › المسودة الذكية #12\n"
    assert _is_template_draft(text, _markup("✏️ تعديل الصيدليات", "📄 معاينة / تصدير Word"))


def test_legacy_draft_is_not_mistaken_for_template_draft() -> None:
    text = "⚙️ لوحة الإدارة › المسودة الذكية #12\n"
    assert not _is_template_draft(text, _markup("✏️ تعديل الجدول", "📄 تحميل Word"))


def test_removed_smart_template_handlers_are_not_registered_at_runtime() -> None:
    source = Path("app/handlers/__init__.py").read_text(encoding="utf-8")
    assert "smart_schedule_template_ui as" not in source
    assert "smart_schedule_template_ui_guard as" not in source
