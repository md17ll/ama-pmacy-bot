from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from app import repositories
from app.config import Settings
from app.db import Database
from app.services import excel
from app.services.excel import parse_pharmacies_workbook
from app.utils import parse_time_value


def _settings(database_url: str) -> Settings:
    return Settings(
        bot_token="test-token",
        gemini_api_key=None,
        database_url=database_url,
        owner_ids=(1,),
        timezone_name="Asia/Damascus",
        run_mode="polling",
        webhook_base_url=None,
        webhook_path="/telegram/webhook",
        webhook_secret=None,
        port=8080,
        gemini_model="gemini-test",
        log_level="INFO",
    )


def test_arabic_time_markers_and_midnight() -> None:
    assert parse_time_value("8 م").hour == 20
    assert parse_time_value("8 مساءً").hour == 20
    assert parse_time_value("8 ص").hour == 8
    assert parse_time_value("منتصف الليل").hour == 0


def test_xlsx_archive_rejects_unsafe_path() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("../escape.xml", "x")
    with pytest.raises(ValueError, match="غير آمن"):
        excel._validate_xlsx_archive(buffer.getvalue())


def test_xlsx_archive_rejects_large_uncompressed_content(monkeypatch) -> None:
    monkeypatch.setattr(excel, "MAX_XLSX_UNCOMPRESSED_BYTES", 1024)
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/large.xml", "0" * 2048)
    with pytest.raises(ValueError, match="فك الضغط"):
        excel._validate_xlsx_archive(buffer.getvalue())


def test_pharmacy_status_import_and_validation() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["اسم الصيدلية", "العنوان", "الحالة"])
    sheet.append(["صيدلية الأمل", "شارع البلدية", "مغلقة مؤقتاً"])
    buffer = BytesIO()
    workbook.save(buffer)

    rows = parse_pharmacies_workbook(buffer.getvalue())
    assert rows[0]["status"] == "temporarily_closed"


def test_shift_reactivation_and_public_visibility(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'reactivate.db'}"))
        await db.init()
        try:
            now = datetime.now(UTC).replace(microsecond=0)
            end = now + timedelta(hours=12)
            async with db.session_factory() as session:
                active = await repositories.create_pharmacy(
                    session,
                    name="صيدلية الشفاء",
                    address="الشارع الرئيسي",
                    admin_id=1,
                )
                inactive = await repositories.create_pharmacy(
                    session,
                    name="صيدلية مغلقة",
                    address="شارع آخر",
                    status="inactive",
                    admin_id=1,
                )
                first = await repositories.create_shift(
                    session,
                    pharmacy_id=active.id,
                    start_at=now,
                    end_at=end,
                    admin_id=1,
                )
                await repositories.create_shift(
                    session,
                    pharmacy_id=inactive.id,
                    start_at=now,
                    end_at=end,
                    admin_id=1,
                )
                await repositories.delete_shift(session, first.id, 1)
                restored = await repositories.create_shift(
                    session,
                    pharmacy_id=active.id,
                    start_at=now,
                    end_at=end,
                    admin_id=1,
                )
                assert restored.id == first.id
                assert restored.active is True

                public = await repositories.list_shifts_between(
                    session,
                    now - timedelta(minutes=1),
                    end + timedelta(minutes=1),
                    include_inactive_pharmacies=False,
                )
                assert [shift.pharmacy.name for shift in public] == ["صيدلية الشفاء"]

                public_search = await repositories.search_pharmacies(
                    session,
                    "مغلقة",
                    include_inactive=False,
                )
                assert public_search == []
                assert await repositories.next_shift_for_pharmacy(
                    session,
                    inactive.id,
                    now - timedelta(minutes=1),
                ) is None
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_replace_publish_reuses_exact_shift(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'publish.db'}"))
        await db.init()
        try:
            start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
            end = start + timedelta(hours=12)
            async with db.session_factory() as session:
                pharmacy = await repositories.create_pharmacy(
                    session,
                    name="صيدلية النور",
                    address="وسط المدينة",
                    admin_id=1,
                )
                original = await repositories.create_shift(
                    session,
                    pharmacy_id=pharmacy.id,
                    start_at=start,
                    end_at=end,
                    admin_id=1,
                )
                batch = await repositories.create_import_batch(
                    session,
                    source_type="excel",
                    source_name="test.xlsx",
                    source_file_id=None,
                    created_by=1,
                    rows=[
                        {
                            "row_number": 2,
                            "raw_pharmacy_name": pharmacy.name,
                            "matched_pharmacy_id": pharmacy.id,
                            "start_at": start,
                            "end_at": end,
                            "confidence": 100.0,
                            "status": "ready",
                            "errors": [],
                            "raw_data": {},
                        }
                    ],
                )
                inserted, removed = await repositories.publish_import_batch(
                    session,
                    batch.id,
                    admin_id=1,
                    replace_period=True,
                )
                assert (inserted, removed) == (1, 1)
                active = await repositories.list_shifts_between(
                    session,
                    start - timedelta(minutes=1),
                    end + timedelta(minutes=1),
                )
                assert len(active) == 1
                assert active[0].id == original.id
                assert active[0].import_batch_id == batch.id
        finally:
            await db.dispose()

    asyncio.run(scenario())
