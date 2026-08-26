from __future__ import annotations

import logging
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings, load_settings
from .helix import HelixClient, HelixError
from .playback import PlaybackManager
from .storage import GuildBinding, Storage

LOG = logging.getLogger("helixbot")


class HelixBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.helix = HelixClient(settings.helix_base_url)
        self.playback = PlaybackManager(self, self.storage, self.helix, settings.volume)
        self._commands_synced = False

    async def setup_hook(self) -> None:
        await self.storage.initialize()
        await self.helix.start()
        await self.add_cog(HelixCommands(self))

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

            # Development-guild mode is exclusive. Remove any previously
            # registered global commands so Discord does not show both the
            # global and guild-scoped copies while developing.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()

            self._commands_synced = True
            LOG.info(
                "Synced slash commands only to development guild %s and cleared global commands",
                self.settings.dev_guild_id,
            )
        else:
            await self.tree.sync()
            self._commands_synced = True
            LOG.info("Synced global slash commands")

    async def close(self) -> None:
        await self.playback.close()
        await self.helix.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user:
            LOG.info("Logged in as %s (%s)", self.user, self.user.id)


class HelixCommands(commands.Cog):
    helix_group = app_commands.Group(name="helix", description="Connect Discord to a Helix lobby")

    def __init__(self, bot: HelixBot) -> None:
        self.bot = bot

    def _guild_id(self, interaction: discord.Interaction) -> int | None:
        return interaction.guild_id

    @helix_group.command(name="link", description="Link this Discord server to a Helix lobby")
    @app_commands.describe(
        code="Five-letter Helix lobby code",
        password="Lobby password, if the lobby is protected",
    )
    async def link(self, interaction: discord.Interaction, code: str, password: str | None = None) -> None:
        guild_id = self._guild_id(interaction)
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a Discord server.", ephemeral=True)
            return

        code = code.strip().upper()
        if len(code) != 5 or not code.isalpha():
            await interaction.response.send_message("Lobby codes are exactly five letters.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        old = await self.bot.storage.get_binding(guild_id)
        nickname = f"HelixBot • {interaction.guild.name}"[:80]

        try:
            joined = await self.bot.helix.join_lobby(code, nickname, password)
        except HelixError as exc:
            await interaction.followup.send(f"Could not join Helix lobby **{code}**: {exc}", ephemeral=True)
            return

        binding = GuildBinding(
            guild_id=guild_id,
            lobby_id=joined.lobby_id,
            lobby_code=joined.lobby_code,
            guest_token=joined.guest_token,
            member_id=joined.member_id,
            voice_channel_id=old.voice_channel_id if old else None,
        )
        await self.bot.playback.stop(guild_id)
        await self.bot.storage.save_binding(binding)

        if old and (old.lobby_id != binding.lobby_id or old.guest_token != binding.guest_token):
            try:
                await self.bot.helix.leave_lobby(old.lobby_id, old.guest_token)
            except HelixError:
                LOG.warning("Could not deactivate previous Helix lobby membership for guild %s", guild_id)

        voice = interaction.guild.voice_client
        if voice and voice.is_connected():
            await self.bot.playback.start(guild_id)

        await interaction.followup.send(
            f"Linked this server to **{joined.lobby_name}** (`{joined.lobby_code}`).",
            ephemeral=True,
        )

    @helix_group.command(name="joinme", description="Join your current voice channel")
    async def joinme(self, interaction: discord.Interaction) -> None:
        guild_id = self._guild_id(interaction)
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a Discord server.", ephemeral=True)
            return

        binding = await self.bot.storage.get_binding(guild_id)
        if binding is None:
            await interaction.response.send_message("This server is not linked to a Helix lobby. Use `/helix link` first.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("Join a voice channel first, then run `/helix joinme`.", ephemeral=True)
            return

        channel = member.voice.channel
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await interaction.response.send_message("That voice channel type is not supported.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        voice = interaction.guild.voice_client
        try:
            if voice and voice.is_connected():
                if voice.channel.id != channel.id:
                    await voice.move_to(channel)
            else:
                await channel.connect(self_deaf=True)
        except discord.DiscordException as exc:
            await interaction.followup.send(f"I could not join that voice channel: {exc}", ephemeral=True)
            return

        await self.bot.storage.set_voice_channel(guild_id, channel.id)
        await self.bot.playback.start(guild_id)
        await interaction.followup.send(
            f"Connected to **{channel.name}** and listening to the linked Helix lobby.",
            ephemeral=True,
        )

    @helix_group.command(name="leaveme", description="Leave the current Discord voice channel")
    async def leaveme(self, interaction: discord.Interaction) -> None:
        guild_id = self._guild_id(interaction)
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a Discord server.", ephemeral=True)
            return

        await self.bot.playback.stop(guild_id)
        voice = interaction.guild.voice_client
        if voice and voice.is_connected():
            await voice.disconnect(force=False)
        await self.bot.storage.set_voice_channel(guild_id, None)
        await interaction.response.send_message("Disconnected from voice. The Helix lobby link is still saved.", ephemeral=True)

    @helix_group.command(name="status", description="Show this server's Helix connection")
    async def status(self, interaction: discord.Interaction) -> None:
        guild_id = self._guild_id(interaction)
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a Discord server.", ephemeral=True)
            return

        binding = await self.bot.storage.get_binding(guild_id)
        if binding is None:
            await interaction.response.send_message("This server is not linked to a Helix lobby.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self.bot.helix.get_lobby_state(binding.lobby_id, binding.guest_token)
        except HelixError as exc:
            await interaction.followup.send(
                f"Linked to `{binding.lobby_code}`, but Helix could not validate the saved session: {exc}",
                ephemeral=True,
            )
            return

        now = state.get("now_playing") or {}
        title = str(now.get("title") or "Nothing")
        artist = str(now.get("artist") or "")
        playing = bool(state.get("is_playing"))
        voice = interaction.guild.voice_client
        voice_text = f"{voice.channel.name}" if voice and voice.is_connected() else "Not connected"

        track_text = title if not artist else f"{title} — {artist}"
        await interaction.followup.send(
            "\n".join(
                [
                    f"**Lobby:** {state.get('name') or 'Shared Lobby'} (`{binding.lobby_code}`)",
                    f"**Helix:** {self.bot.settings.helix_base_url}",
                    f"**Playback:** {'Playing' if playing else 'Paused'} — {track_text}",
                    f"**Voice:** {voice_text}",
                ]
            ),
            ephemeral=True,
        )

    @helix_group.command(name="invite", description="Share the linked Helix lobby invite")
    async def invite(self, interaction: discord.Interaction) -> None:
        guild_id = self._guild_id(interaction)
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a Discord server.", ephemeral=True)
            return

        binding = await self.bot.storage.get_binding(guild_id)
        if binding is None:
            await interaction.response.send_message("This server is not linked to a Helix lobby.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            state = await self.bot.helix.get_lobby_state(binding.lobby_id, binding.guest_token)
        except HelixError as exc:
            await interaction.followup.send(
                f"I couldn't validate the linked Helix lobby: {exc}",
                ephemeral=True,
            )
            return

        lobby_name = str(state.get("name") or "Shared Lobby")
        has_password = bool(state.get("has_password"))
        invite_url = f"{self.bot.settings.helix_base_url}/join/{binding.lobby_code}"
        protection = "Password required." if has_password else "No password required — anyone with this link can join."

        await interaction.followup.send(
            "\n".join(
                [
                    f"**{lobby_name}**",
                    f"Join code: `{binding.lobby_code}`",
                    invite_url,
                    protection,
                ]
            )
        )


    @helix_group.command(name="help", description="Explain HelixBot and show available commands")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "\n".join(
                [
                    "**HelixBot**",
                    "HelixBot connects Discord voice to **Helix**, a self-hosted music system. In Helix, people can create **lobbies** — shared listening rooms where everyone hears the same queue and playback state.",
                    "",
                    "This Discord server can be linked to one Helix lobby. Once linked, HelixBot can join a voice channel and mirror whatever that lobby is currently playing. Helix remains in control of playback; HelixBot does not play, pause, skip, or seek the lobby from Discord.",
                    "",
                    "**Commands**",
                    "`/helix link CODE [password]` — Link this Discord server to a Helix lobby.",
                    "`/helix joinme` — Join your current voice channel and mirror the linked lobby.",
                    "`/helix leaveme` — Leave Discord voice without unlinking the lobby.",
                    "`/helix invite` — Post the linked lobby's join code and invite link.",
                    "`/helix status` — Show the linked lobby and current playback state.",
                    "`/helix unlink` — Disconnect this Discord server from the Helix lobby.",
                    "`/helix help` — Show this help message.",
                ]
            ),
            ephemeral=True,
        )

    @helix_group.command(name="unlink", description="Remove this server's Helix lobby link")
    async def unlink(self, interaction: discord.Interaction) -> None:
        guild_id = self._guild_id(interaction)
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a Discord server.", ephemeral=True)
            return

        binding = await self.bot.storage.get_binding(guild_id)
        if binding is None:
            await interaction.response.send_message("This server is not linked to a Helix lobby.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.bot.playback.stop(guild_id)
        voice = interaction.guild.voice_client
        if voice and voice.is_connected():
            await voice.disconnect(force=False)

        try:
            await self.bot.helix.leave_lobby(binding.lobby_id, binding.guest_token)
        except HelixError as exc:
            LOG.warning("Helix leave failed while unlinking guild %s: %s", guild_id, exc)

        await self.bot.storage.delete_binding(guild_id)
        await interaction.followup.send("Unlinked this Discord server from Helix.", ephemeral=True)


async def _tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    LOG.exception("Slash command failed", exc_info=error)
    message = "HelixBot hit an unexpected error while processing that command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def run() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot = HelixBot(settings)
    bot.tree.on_error = _tree_error
    bot.run(settings.discord_token, log_handler=None)
