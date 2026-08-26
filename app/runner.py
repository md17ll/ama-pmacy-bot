from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.config import get_settings
from app.db import Database
from app.fsm_storage import DatabaseFSMStorage
from app.handlers import (
    admin,
    admins,
    exports,
    imports,
    missing_pharmacies,
    pharmacies,
    premium,
    shifts,
    user,
    word_imports,
)
from app.middlewares import AntiSpamMiddleware
from app.repositories import sync_owner_admins
from app.services.gemini import GeminiScheduleReader
from app.services.scheduler import schedule_expiry_watch


logger = logging.getLogger(__name__)
TELEGRAM_ERRORS = (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError)


async def _best_effort_telegram_call(
    label: str,
    operation: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 3,
) -> bool:
    """Retry optional Telegram setup calls without killing the bot process."""
    for attempt in range(1, attempts + 1):
        try:
            await operation()
            return True
        except TELEGRAM_ERRORS as exc:
            if attempt >= attempts:
                logger.warning(
                    "%s failed after %s attempts; continuing startup: %s",
                    label,
                    attempts,
                    exc,
                )
                return False
            delay = min(2 ** (attempt - 1), 8)
            logger.warning(
                "%s failed (attempt %s/%s): %s; retrying in %ss",
                label,
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    return False


def build_dispatcher(db: Database) -> Dispatcher:
    dispatcher = Dispatcher(
        storage=DatabaseFSMStorage(db),
        events_isolation=SimpleEventIsolation(),
    )
    anti_spam = AntiSpamMiddleware()
    dispatcher.message.outer_middleware(anti_spam)
    dispatcher.callback_query.outer_middleware(anti_spam)

    dispatcher.include_router(premium.router)
    dispatcher.include_router(missing_pharmacies.router)
    dispatcher.include_router(word_imports.router)
    dispatcher.include_router(imports.router)
    dispatcher.include_router(pharmacies.router)
    dispatcher.include_router(shifts.router)
    dispatcher.include_router(admins.router)
    dispatcher.include_router(exports.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(user.router)
    return dispatcher


async def _prepare_database(db: Database, owner_ids: tuple[int, ...]) -> None:
    await db.init()
    async with db.session_factory() as session:
        await sync_owner_admins(session, owner_ids)


async def _configure_telegram(bot: Bot, *, delete_webhook: bool) -> None:
    commands = [
        BotCommand(command="start", description="فتح البوت"),
        BotCommand(command="admin", description="لوحة الإدارة"),
    ]
    await _best_effort_telegram_call(
        "Setting Telegram bot commands",
        lambda: bot.set_my_commands(commands),
        attempts=1,
    )
    if delete_webhook:
        await _best_effort_telegram_call(
            "Deleting an old Telegram webhook",
            lambda: bot.delete_webhook(drop_pending_updates=False),
            attempts=1,
        )


async def run_polling() -> None:
    settings = get_settings()
    settings.validate_runtime()
    db = Database(settings)
    gemini_reader = GeminiScheduleReader(settings.gemini_api_key, settings.gemini_model)
    dispatcher = build_dispatcher(db)
    await _prepare_database(db, settings.owner_ids)

    retry_delay = 5
    try:
        while True:
            bot = Bot(
                settings.bot_token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            watch_task: asyncio.Task[None] | None = None
            try:
                logger.info("Checking Telegram API connection")
                bot_info = await bot.get_me()
                logger.info(
                    "Telegram API connected as @%s",
                    bot_info.username or bot_info.id,
                )
                await _configure_telegram(bot, delete_webhook=True)

                watch_task = asyncio.create_task(
                    schedule_expiry_watch(bot, db, settings)
                )
                logger.info("Starting Telegram long polling")
                retry_delay = 5
                await dispatcher.start_polling(
                    bot,
                    db=db,
                    settings=settings,
                    gemini_reader=gemini_reader,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                    handle_signals=False,
                    close_bot_session=False,
                )
                logger.warning(
                    "Telegram polling stopped unexpectedly; reconnecting in %ss",
                    retry_delay,
                )
            except TELEGRAM_ERRORS as exc:
                logger.warning(
                    "Telegram API/polling unavailable: %s; reconnecting in %ss",
                    exc,
                    retry_delay,
                )
            finally:
                if watch_task is not None:
                    watch_task.cancel()
                    await asyncio.gather(watch_task, return_exceptions=True)
                await bot.session.close()

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
    finally:
        await db.dispose()


async def run_webhook() -> None:
    settings = get_settings()
    settings.validate_runtime()
    db = Database(settings)
    gemini_reader = GeminiScheduleReader(settings.gemini_api_key, settings.gemini_model)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = build_dispatcher(db)
    dispatcher.workflow_data.update(
        db=db,
        settings=settings,
        gemini_reader=gemini_reader,
    )
    await _prepare_database(db, settings.owner_ids)
    await _configure_telegram(bot, delete_webhook=False)
    await bot.set_webhook(
        settings.webhook_url,
        secret_token=settings.webhook_secret,
        allowed_updates=dispatcher.resolve_used_update_types(),
        drop_pending_updates=False,
    )

    app = web.Application()
    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "amuda-pharmacy-bot"})

    async def cleanup(_app: web.Application) -> None:
        await bot.session.close()
        await db.dispose()

    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.webhook_secret,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)
    app.on_cleanup.append(cleanup)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=None, port=settings.port)
    await site.start()
    logger.info("Webhook server started on port %s", settings.port)
    await asyncio.Event().wait()


def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if settings.run_mode == "webhook":
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())
