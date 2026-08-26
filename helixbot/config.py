from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_token: str
    helix_base_url: str
    db_path: str
    dev_guild_id: int | None
    log_level: str
    volume: float


def load_settings() -> Settings:
    load_dotenv()

    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is required")

    base_url = (os.getenv("HELIX_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("HELIX_BASE_URL is required")
    if not base_url.startswith(("http://", "https://")):
        raise RuntimeError("HELIX_BASE_URL must start with http:// or https://")

    raw_dev_guild = (os.getenv("DISCORD_DEV_GUILD_ID") or "").strip()
    dev_guild_id = int(raw_dev_guild) if raw_dev_guild else None

    raw_volume = (os.getenv("HELIXBOT_VOLUME") or "0.40").strip()
    try:
        volume = float(raw_volume)
    except ValueError as exc:
        raise RuntimeError("HELIXBOT_VOLUME must be a number between 0.0 and 1.0") from exc
    if not 0.0 <= volume <= 1.0:
        raise RuntimeError("HELIXBOT_VOLUME must be between 0.0 and 1.0")

    return Settings(
        discord_token=token,
        helix_base_url=base_url,
        db_path=(os.getenv("HELIXBOT_DB_PATH") or "/data/helixbot.sqlite3").strip(),
        dev_guild_id=dev_guild_id,
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        volume=volume,
    )
