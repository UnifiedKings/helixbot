from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuildBinding:
    guild_id: int
    lobby_id: str
    lobby_code: str
    guest_token: str
    member_id: str
    voice_channel_id: int | None


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_bindings (
                    guild_id INTEGER PRIMARY KEY,
                    lobby_id TEXT NOT NULL,
                    lobby_code TEXT NOT NULL,
                    guest_token TEXT NOT NULL,
                    member_id TEXT NOT NULL,
                    voice_channel_id INTEGER NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.commit()

    async def get_binding(self, guild_id: int) -> GuildBinding | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_binding_sync, guild_id)

    def _get_binding_sync(self, guild_id: int) -> GuildBinding | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                """
                SELECT guild_id, lobby_id, lobby_code, guest_token, member_id, voice_channel_id
                FROM guild_bindings
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        return GuildBinding(*row) if row else None

    async def save_binding(self, binding: GuildBinding) -> None:
        async with self._lock:
            await asyncio.to_thread(self._save_binding_sync, binding)

    def _save_binding_sync(self, binding: GuildBinding) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                INSERT INTO guild_bindings (
                    guild_id, lobby_id, lobby_code, guest_token, member_id, voice_channel_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    lobby_id = excluded.lobby_id,
                    lobby_code = excluded.lobby_code,
                    guest_token = excluded.guest_token,
                    member_id = excluded.member_id,
                    voice_channel_id = excluded.voice_channel_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    binding.guild_id,
                    binding.lobby_id,
                    binding.lobby_code,
                    binding.guest_token,
                    binding.member_id,
                    binding.voice_channel_id,
                ),
            )
            db.commit()

    async def set_voice_channel(self, guild_id: int, voice_channel_id: int | None) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_voice_channel_sync, guild_id, voice_channel_id)

    def _set_voice_channel_sync(self, guild_id: int, voice_channel_id: int | None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                UPDATE guild_bindings
                SET voice_channel_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ?
                """,
                (voice_channel_id, guild_id),
            )
            db.commit()

    async def delete_binding(self, guild_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_binding_sync, guild_id)

    def _delete_binding_sync(self, guild_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM guild_bindings WHERE guild_id = ?", (guild_id,))
            db.commit()
