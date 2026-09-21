from __future__ import annotations

import asyncio
from types import SimpleNamespace

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
