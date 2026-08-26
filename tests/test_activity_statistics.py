from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from app import keyboards, repositories
from app.config import Settings
from app.db import Database
from app.utils import utcnow


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
        gemini_model="unused",
        log_level="INFO",
    )


def test_activity_statistics_count_unique_users_and_total_clicks(tmp_path) -> None:
    async def scenario() -> None:
        db = Database(_settings(f"sqlite+aiosqlite:///{tmp_path / 'activity.db'}"))
        await db.init()
        try:
            async with db.session_factory() as session:
                for user_id, name in ((101, "أحمد"), (202, "سارة")):
                    await repositories.upsert_user(
                        session,
                        telegram_id=user_id,
                        username=None,
                        first_name=name,
                        last_name=None,
                        language_code="ar",
                        source=None,
                    )

                for user_id in (101, 101, 202):
                    await repositories.record_activity(
                        session,
                        user_id,
                        "button_click",
                        {
                            "action": "u:today",
                            "callback_data": "u:today",
                            "button_text": "📅 صيدليات اليوم",
                            "scope": "user",
                        },
                    )
                await repositories.record_activity(
                    session,
                    101,
                    "button_click",
                    {
                        "action": "a:stats",
                        "callback_data": "a:stats",
                        "button_text": "📊 الإحصائيات",
                        "scope": "admin",
                    },
                )
                await repositories.record_activity(
                    session,
                    202,
                    "message_activity",
                    {"kind": "text"},
                )
                await repositories.record_usage_event(
                    session,
                    202,
                    "user_search",
                    {"query": "غير موجودة", "pharmacy_id": None},
                )

                since = utcnow() - timedelta(hours=1)
                overview = await repositories.activity_overview(session, since=since)
                buttons = await repositories.button_usage_statistics(session, since=since)
                active, total = await repositories.list_active_users(session, since=since)

            assert overview["active_users"] == 2
            assert overview["button_users"] == 2
            assert overview["button_clicks"] == 3
            assert overview["searches"] == 1
            assert overview["empty_searches"] == 1
            assert buttons == [
                {
                    "action": "u:today",
                    "button_text": "📅 صيدليات اليوم",
                    "clicks": 3,
                    "users": 2,
                }
            ]
            assert total == 2
            by_id = {item["telegram_id"]: item for item in active}
            assert by_id[101]["buttons"] == 3
            assert by_id[202]["buttons"] == 1
            assert by_id[202]["messages"] == 1
            assert by_id[202]["searches"] == 1
        finally:
            await db.dispose()

    asyncio.run(scenario())


def test_removed_features_are_not_in_admin_keyboards_or_runtime_registration() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app import keyboards; "
                "print('\\n'.join(button.text for markup in "
                "(keyboards.admin_home(), keyboards.admin_import()) "
                "for row in markup.inline_keyboard for button in row))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "GPT" not in result.stdout
    assert "Gemini" not in result.stdout
    assert "رفع ملف Excel" not in result.stdout
    assert "مولّد الجداول الذكي" not in result.stdout

    handlers_init = Path("app/handlers/__init__.py").read_text(encoding="utf-8")
    runner = Path("app/runner.py").read_text(encoding="utf-8")
    imports_handler = Path("app/handlers/imports.py").read_text(encoding="utf-8")
    assert "smart_schedule" not in handlers_init
    assert "GeminiScheduleReader" not in runner
    assert "gemini_receive_photo" not in imports_handler
    assert "excel_receive_document" not in imports_handler
