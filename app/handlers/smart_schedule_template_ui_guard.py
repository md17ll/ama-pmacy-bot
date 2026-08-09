from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.handlers import smart_schedules as smart
from app.telegram_utils import safe_edit as _raw_safe_edit


_PREVIOUS_SAFE_EDIT = smart.safe_edit
_TEMPLATE_DRAFT_TITLE = "لوحة الإدارة › المسودة الذكية #"
_TEMPLATE_EDIT_BUTTON = "✏️ تعديل الصيدليات"


def _is_template_draft(text: str, markup: InlineKeyboardMarkup | None) -> bool:
    if _TEMPLATE_DRAFT_TITLE not in text or markup is None:
        return False
    return any(
        button.text == _TEMPLATE_EDIT_BUTTON
        for row in markup.inline_keyboard
        for button in row
    )


async def _template_aware_safe_edit(
    target,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Keep the approved template draft UI from being rewritten by the legacy simplifier."""
    if _is_template_draft(text, reply_markup):
        return await _raw_safe_edit(target, text, reply_markup)
    return await _PREVIOUS_SAFE_EDIT(target, text, reply_markup)


# smart_schedule_template_ui renders through smart_schedules.safe_edit at runtime.
# Install this after both the legacy simplifier and the approved template UI so
# only the approved draft screen bypasses the legacy button/text rewrite.
smart.safe_edit = _template_aware_safe_edit
