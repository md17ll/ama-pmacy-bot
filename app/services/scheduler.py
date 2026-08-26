from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot

from app import repositories
from app.config import Settings
from app.db import Database
from app.utils import format_date_ar, format_duration, utcnow


logger = logging.getLogger(__name__)


async def schedule_expiry_watch(bot: Bot, db: Database, settings: Settings) -> None:
    """Send one schedule-expiry alert per local day to owners/admins."""
    while True:
        try:
            await check_schedule_expiry(bot, db, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Schedule expiry watch failed")
        await asyncio.sleep(3600)


async def check_schedule_expiry(bot: Bot, db: Database, settings: Settings) -> None:
    now = utcnow()
    local_today = now.astimezone(settings.timezone).date()
    key = "schedule_expiry_last_alert_date"
    async with db.session_factory() as session:
        last_sent = await repositories.get_setting(session, key)
        if last_sent == local_today.isoformat():
            return
        latest = await repositories.latest_shift_end(session)
        if latest is None:
            message = "🚨 لا توجد مناوبات منشورة حالياً. يرجى رفع الجدول قبل استخدام البوت من الجمهور."
        else:
            remaining = latest - now
            if remaining > timedelta(days=2):
                return
            if remaining.total_seconds() <= 0:
                message = "🚨 انتهى جدول المناوبات المنشور. يرجى رفع جدول جديد فوراً."
            else:
                message = (
                    "⚠️ جدول المناوبات سينتهي قريباً.\n\n"
                    f"📅 آخر يوم: {format_date_ar(latest, settings.timezone)}\n"
                    f"⏳ المتبقي: {format_duration(remaining)}"
                )
        recipients = await repositories.schedule_alert_recipients(session)
        await repositories.set_setting(session, key, local_today.isoformat())
    for recipient in recipients:
        try:
            await bot.send_message(recipient, message)
        except Exception:
            logger.warning("Could not send schedule alert to %s", recipient)
