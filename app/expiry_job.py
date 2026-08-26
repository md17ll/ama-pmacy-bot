from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.db import Database
from app.services.scheduler import check_schedule_expiry


async def run_once() -> None:
    """Run the expiry notification check once and exit for Railway Cron."""
    settings = get_settings()
    settings.validate_runtime()
    db = Database(settings)
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await db.init()
        await check_schedule_expiry(bot, db, settings)
    finally:
        await bot.session.close()
        await db.dispose()


def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run_once())


if __name__ == "__main__":
    run()
