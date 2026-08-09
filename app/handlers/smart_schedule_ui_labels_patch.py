from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.handlers import smart_schedule_ui as ui


_ORIGINAL_DRAFT_UI = ui._draft_ui
_ORIGINAL_PUBLISHED_UI = ui._published_ui


def _renamed_markup(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            text = button.text
            callback_data = button.callback_data or ""
            if callback_data.startswith("a:smart:edit:"):
                text = "✏️ تعديل الصيدليات"
            elif callback_data.startswith("a:smart:word:"):
                text = "📄 معاينة / تحميل Word"
            new_row.append(button.model_copy(update={"text": text}))
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _draft_ui(text, markup):
    rendered_text, rendered_markup = _ORIGINAL_DRAFT_UI(text, markup)
    return rendered_text, _renamed_markup(rendered_markup)


def _published_ui(text, markup):
    rendered_text, rendered_markup = _ORIGINAL_PUBLISHED_UI(text, markup)
    return rendered_text, _renamed_markup(rendered_markup)


ui._draft_ui = _draft_ui
ui._published_ui = _published_ui
