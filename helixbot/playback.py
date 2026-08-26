from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

from .helix import HelixClient, HelixError
from .storage import GuildBinding, Storage

LOG = logging.getLogger("helixbot.playback")

# Discord and browser playback do not need sample-perfect synchronization. A
# multi-second tolerance avoids repeatedly restarting FFmpeg for normal network
# and voice-gateway jitter, while explicit Helix seeks are still applied
# immediately because position_updated_at changes.
DRIFT_RESTART_MS = 5_000
RECONNECT_DELAY_SECONDS = 1.5
STREAM_FAILURE_BACKOFF_SECONDS = 2.0
SEEK_DEBOUNCE_SECONDS = 0.45
SOURCE_TEARDOWN_GRACE_SECONDS = 0.10


@dataclass
class PlaybackClock:
    item_id: str = ""
    position_ms: int = 0
    started_at: float = 0.0
    running: bool = False

    def current_ms(self) -> int:
        if not self.running:
            return max(0, self.position_ms)
        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        return max(0, self.position_ms + elapsed_ms)

    def start(self, item_id: str, position_ms: int) -> None:
        self.item_id = item_id
        self.position_ms = max(0, int(position_ms))
        self.started_at = time.monotonic()
        self.running = True

    def pause(self) -> None:
        self.position_ms = self.current_ms()
        self.started_at = 0.0
        self.running = False

    def resume(self) -> None:
        self.started_at = time.monotonic()
        self.running = True

    def clear(self) -> None:
        self.item_id = ""
        self.position_ms = 0
        self.started_at = 0.0
        self.running = False


class GuildPlaybackSession:
    def __init__(
        self,
        bot: discord.Client,
        storage: Storage,
        helix: HelixClient,
        guild_id: int,
        volume: float,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.helix = helix
        self.guild_id = guild_id
        self.volume = volume
        self.task: asyncio.Task[None] | None = None
        self.binding: GuildBinding | None = None
        self.state: dict[str, Any] | None = None
        self.clock = PlaybackClock()
        self.position_stamp = ""
        self.generation = 0
        self._stopping = False
        self._state_lock = asyncio.Lock()
        self._source_started_at = 0.0
        self._retry_not_before = 0.0
        self._seek_task: asyncio.Task[None] | None = None
        self._pending_seek_item_id = ""
        self._pending_seek_position_ms = 0

    async def start(self) -> None:
        await self.stop()
        self._stopping = False
        self.task = asyncio.create_task(self._run(), name=f"helixbot-playback-{self.guild_id}")

    async def stop(self) -> None:
        self._stopping = True
        self.generation += 1
        task = self.task
        self.task = None
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._cancel_pending_seek()
        self._stop_voice_source()
        self.clock.clear()
        self.state = None
        self.binding = None
        self.position_stamp = ""

    def _voice(self) -> discord.VoiceClient | None:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return None
        voice = guild.voice_client
        return voice if isinstance(voice, discord.VoiceClient) else None

    async def _run(self) -> None:
        while not self._stopping:
            binding = await self.storage.get_binding(self.guild_id)
            voice = self._voice()
            if binding is None or voice is None or not voice.is_connected():
                return
            self.binding = binding

            try:
                # Reconcile immediately instead of waiting for the first socket
                # broadcast. This also verifies that the saved guest token is
                # still valid before opening the realtime connection.
                initial = await self.helix.get_lobby_state(binding.lobby_id, binding.guest_token)
                await self._apply_state(initial)

                async with self.helix.lobby_websocket(binding.lobby_id, binding.guest_token) as ws:
                    LOG.info("Guild %s subscribed to Helix lobby %s", self.guild_id, binding.lobby_code)
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for message in ws:
                            if self._stopping:
                                return
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = message.json()
                                except Exception:
                                    continue
                                if payload.get("type") == "lobby.state" and isinstance(payload.get("state"), dict):
                                    await self._apply_state(payload["state"])
                            elif message.type in {
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            }:
                                break
                    finally:
                        ping_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await ping_task
            except asyncio.CancelledError:
                raise
            except (HelixError, aiohttp.ClientError, OSError) as exc:
                LOG.warning("Guild %s Helix playback connection lost: %s", self.guild_id, exc)
            except Exception:
                LOG.exception("Guild %s playback session failed", self.guild_id)

            if not self._stopping:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # Helix's lobby socket updates guest last_seen_at when it receives text.
        # Match the browser client's application-level ping rather than relying
        # only on WebSocket protocol PING frames.
        while not self._stopping and not ws.closed:
            await asyncio.sleep(20)
            if not ws.closed:
                await ws.send_str("ping")

    @staticmethod
    def _desired_position_ms(state: dict[str, Any]) -> int:
        position = max(0, int(state.get("effective_position_ms") or 0))
        if not state.get("is_playing"):
            return position
        server_time_ms = int(state.get("server_time_ms") or 0)
        if server_time_ms <= 0:
            return position
        # server_time_ms is wall-clock epoch time. Add transit time so joining a
        # lobby does not start at the slightly stale WebSocket snapshot position.
        transit_ms = max(0, int(time.time() * 1000) - server_time_ms)
        return position + min(transit_ms, 10_000)

    async def _apply_state(self, state: dict[str, Any]) -> None:
        async with self._state_lock:
            if self._stopping or self.binding is None:
                return
            self.state = state
            voice = self._voice()
            if voice is None or not voice.is_connected():
                return

            now = state.get("now_playing") or {}
            item_id = str(now.get("id") or "")
            is_playing = bool(state.get("is_playing"))
            position_stamp = str(state.get("position_updated_at") or "")
            desired_ms = self._desired_position_ms(state)

            if not item_id:
                await self._cancel_pending_seek()
                self._stop_voice_source()
                self.clock.clear()
                self.position_stamp = position_stamp
                return

            track_changed = self.clock.item_id != item_id
            explicit_position_change = (
                not track_changed
                and bool(self.position_stamp)
                and position_stamp != self.position_stamp
            )

            if track_changed:
                await self._cancel_pending_seek()
                self.position_stamp = position_stamp
                if is_playing:
                    await self._restart_source(item_id, desired_ms)
                else:
                    self._stop_voice_source()
                    self.clock.item_id = item_id
                    self.clock.position_ms = desired_ms
                    self.clock.running = False
                return

            self.position_stamp = position_stamp

            if not is_playing:
                if voice.is_playing():
                    voice.pause()
                if self.clock.running:
                    self.clock.pause()
                # A host may seek while paused. The next resume must begin from
                # Helix's authoritative paused position, not the old FFmpeg read
                # position, because a paused Discord AudioPlayer does not seek.
                if explicit_position_change:
                    await self._cancel_pending_seek()
                    self._stop_voice_source()
                    self.clock.item_id = item_id
                    self.clock.position_ms = desired_ms
                    self.clock.running = False
                return

            if explicit_position_change:
                self._schedule_seek_restart(item_id, desired_ms)
                return

            # While a seek is waiting for the user to finish scrubbing, ignore
            # normal drift checks. The pending seek task will restart once at the
            # newest requested position instead of thrashing FFmpeg repeatedly.
            if self._seek_task is not None and not self._seek_task.done():
                return

            if voice.is_paused():
                drift = abs(self.clock.current_ms() - desired_ms)
                if drift > DRIFT_RESTART_MS or not self.clock.item_id:
                    await self._restart_source(item_id, desired_ms)
                else:
                    voice.resume()
                    self.clock.resume()
                return

            if not voice.is_playing():
                await self._restart_source(item_id, desired_ms)
                return

            drift = abs(self.clock.current_ms() - desired_ms)
            if drift > DRIFT_RESTART_MS:
                LOG.info(
                    "Resyncing guild %s lobby item %s (drift=%dms)",
                    self.guild_id,
                    item_id,
                    drift,
                )
                await self._restart_source(item_id, desired_ms)


    async def _cancel_pending_seek(self) -> None:
        task = self._seek_task
        self._seek_task = None
        self._pending_seek_item_id = ""
        self._pending_seek_position_ms = 0
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _schedule_seek_restart(self, item_id: str, position_ms: int) -> None:
        self._pending_seek_item_id = item_id
        self._pending_seek_position_ms = max(0, int(position_ms))

        old_task = self._seek_task
        if old_task and not old_task.done():
            old_task.cancel()

        self._seek_task = asyncio.create_task(
            self._debounced_seek_restart(),
            name=f"helixbot-seek-{self.guild_id}",
        )

    async def _debounced_seek_restart(self) -> None:
        try:
            await asyncio.sleep(SEEK_DEBOUNCE_SECONDS)
            async with self._state_lock:
                if self._stopping or self.binding is None:
                    return

                state = self.state or {}
                now = state.get("now_playing") or {}
                item_id = self._pending_seek_item_id
                position_ms = self._pending_seek_position_ms

                if (
                    not item_id
                    or str(now.get("id") or "") != item_id
                    or not state.get("is_playing")
                ):
                    return

                # Clear the task reference before restarting so _apply_state does
                # not mistake this seek itself for an outstanding debounce.
                self._seek_task = None
                self._pending_seek_item_id = ""
                self._pending_seek_position_ms = 0
                await self._restart_source(item_id, position_ms)
        except asyncio.CancelledError:
            raise
        finally:
            if self._seek_task is asyncio.current_task():
                self._seek_task = None

    def _stop_voice_source(self) -> None:
        voice = self._voice()
        if voice and (voice.is_playing() or voice.is_paused()):
            # Invalidate the source before stop(), because stop() invokes the
            # AudioPlayer after callback from its worker thread.
            self.generation += 1
            voice.stop()

    async def _restart_source(self, item_id: str, position_ms: int) -> None:
        binding = self.binding
        voice = self._voice()
        if binding is None or voice is None or not voice.is_connected() or self._stopping:
            return

        retry_delay = self._retry_not_before - time.monotonic()
        if retry_delay > 0:
            await asyncio.sleep(retry_delay)
            if self._stopping:
                return

        self._stop_voice_source()
        # discord.py tears down FFmpeg/AudioPlayer on a worker thread. Give the
        # old source a moment to close its pipe before creating the replacement;
        # otherwise rapid source swaps can overlap encoder/resampler teardown.
        await asyncio.sleep(SOURCE_TEARDOWN_GRACE_SECONDS)
        if self._stopping or voice is None or not voice.is_connected():
            return
        self.generation += 1
        generation = self.generation
        seek_seconds = max(0.0, position_ms / 1000.0)
        url = self.helix.stream_url(binding.lobby_id, item_id)

        # FFmpeg's -headers option expects the HTTP header block itself, with a
        # real CRLF terminator. Passing the shell-escaped text ``\r\n`` causes
        # FFmpeg to send a malformed header and Helix correctly responds 401.
        http_headers = f"x-helix-lobby-token: {binding.guest_token}\r\n"
        before_parts = [
            "-nostdin",
            "-reconnect 1",
            "-reconnect_streamed 1",
            "-reconnect_delay_max 5",
            f'-headers "{http_headers}"',
        ]
        # Avoid asking the HTTP source to seek for tiny startup offsets. For a
        # bot joining mid-song, input-side -ss lets FFmpeg use Helix's range
        # support rather than decoding minutes of audio before Discord hears it.
        if seek_seconds >= 1.0:
            before_parts.append(f"-ss {seek_seconds:.3f}")

        try:
            ffmpeg_source = discord.FFmpegPCMAudio(
                url,
                before_options=" ".join(before_parts),
                options="-vn -loglevel warning",
            )
            source = discord.PCMVolumeTransformer(ffmpeg_source, volume=self.volume)
            loop = asyncio.get_running_loop()

            def after(error: Exception | None) -> None:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        self._source_finished(generation, item_id, error)
                    )
                )

            voice.play(source, after=after)
            self._source_started_at = time.monotonic()
            self.clock.start(item_id, position_ms)
            LOG.info(
                "Guild %s playing Helix item %s at %.3fs",
                self.guild_id,
                item_id,
                seek_seconds,
            )
        except Exception as exc:
            LOG.warning("Could not start Discord audio for guild %s: %s", self.guild_id, exc)
            self.clock.item_id = item_id
            self.clock.position_ms = position_ms
            self.clock.running = False

    async def _source_finished(
        self,
        generation: int,
        item_id: str,
        error: Exception | None,
    ) -> None:
        # Replaced/seeking sources deliberately finish too. Only the still-current
        # generation is allowed to report a natural end to Helix.
        if self._stopping or generation != self.generation or self.binding is None:
            return
        self.clock.pause()
        source_runtime = max(0.0, time.monotonic() - self._source_started_at)
        if error:
            LOG.warning("Discord audio source for guild %s ended with error: %s", self.guild_id, error)

        # Authentication/network/decoder failures often make FFmpeg exit almost
        # immediately. Rate-limit the subsequent reconciliation so a persistent
        # failure cannot create a tight process-spawn loop.
        if source_runtime < STREAM_FAILURE_BACKOFF_SECONDS:
            self._retry_not_before = max(
                self._retry_not_before,
                time.monotonic() + STREAM_FAILURE_BACKOFF_SECONDS,
            )

        state = self.state or {}
        now = state.get("now_playing") or {}
        if str(now.get("id") or "") != item_id or not state.get("is_playing"):
            return

        try:
            next_state = await self.helix.report_lobby_ended(
                self.binding.lobby_id,
                self.binding.guest_token,
                item_id,
            )
        except HelixError as exc:
            LOG.warning("Could not report lobby item end for guild %s: %s", self.guild_id, exc)
            next_state = None

        # If the source died early, Helix's /ended endpoint rejects the advance.
        # Reconcile from authoritative state and restart at the correct position.
        if next_state is None:
            try:
                next_state = await self.helix.get_lobby_state(
                    self.binding.lobby_id,
                    self.binding.guest_token,
                )
            except HelixError:
                return
        await self._apply_state(next_state)


class PlaybackManager:
    def __init__(self, bot: discord.Client, storage: Storage, helix: HelixClient, volume: float = 0.10) -> None:
        self.bot = bot
        self.storage = storage
        self.helix = helix
        self.volume = volume
        self.sessions: dict[int, GuildPlaybackSession] = {}

    async def start(self, guild_id: int) -> None:
        session = self.sessions.get(guild_id)
        if session is None:
            session = GuildPlaybackSession(self.bot, self.storage, self.helix, guild_id, self.volume)
            self.sessions[guild_id] = session
        await session.start()

    async def stop(self, guild_id: int) -> None:
        session = self.sessions.pop(guild_id, None)
        if session:
            await session.stop()

    async def close(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        await asyncio.gather(*(session.stop() for session in sessions), return_exceptions=True)
