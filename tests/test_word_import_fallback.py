from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import app.handlers.word_imports as word_imports
from app.handlers.word_imports import DocxDocumentFilter


def test_docx_fallback_filter_accepts_docx_only() -> None:
    async def scenario() -> None:
        filter_ = DocxDocumentFilter()
        assert await filter_(SimpleNamespace(document=SimpleNamespace(file_name="schedule.docx")))
        assert await filter_(SimpleNamespace(document=SimpleNamespace(file_name="SCHEDULE.DOCX")))
        assert not await filter_(SimpleNamespace(document=SimpleNamespace(file_name="schedule.xlsx")))
        assert not await filter_(SimpleNamespace(document=SimpleNamespace(file_name=None)))
        assert not await filter_(SimpleNamespace(document=None))

    asyncio.run(scenario())


def test_docx_handler_has_priority_over_waiting_word_catchall() -> None:
    source = inspect.getsource(word_imports)
    docx_handler = source.index("@router.message(DocxDocumentFilter())")
    wrong_input_handler = source.index("@router.message(AdminImportState.waiting_word)")
    assert docx_handler < wrong_input_handler
