from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo


def _csv_ints(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError as exc:
            raise ValueError(f"OWNER_IDS contains a non-numeric value: {item!r}") from exc
    return tuple(result)


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    return url


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    gemini_api_key: str | None
    database_url: str
    owner_ids: tuple[int, ...]
    timezone_name: str
    run_mode: str
    webhook_base_url: str | None
    webhook_path: str
    webhook_secret: str | None
    port: int
    gemini_model: str
    log_level: str

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def webhook_url(self) -> str | None:
        if not self.webhook_base_url:
            return None
        return self.webhook_base_url.rstrip("/") + "/" + self.webhook_path.lstrip("/")

    def validate_runtime(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        if not self.owner_ids:
            raise RuntimeError("OWNER_IDS must contain at least one Telegram user ID")
        if self.run_mode not in {"polling", "webhook"}:
            raise RuntimeError("RUN_MODE must be either 'polling' or 'webhook'")
        if self.run_mode == "webhook":
            if not self.webhook_url:
                raise RuntimeError("WEBHOOK_BASE_URL is required in webhook mode")
            if not self.webhook_secret:
                raise RuntimeError("WEBHOOK_SECRET is required in webhook mode")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
        database_url=_normalize_database_url(
            os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./amuda_bot.db").strip()
        ),
        owner_ids=_csv_ints(os.getenv("OWNER_IDS", "")),
        timezone_name=os.getenv("TIMEZONE", "Asia/Damascus").strip(),
        run_mode=os.getenv("RUN_MODE", "polling").strip().lower(),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL", "").strip() or None,
        webhook_path=os.getenv("WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook",
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip() or None,
        port=int(os.getenv("PORT", "8080")),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
