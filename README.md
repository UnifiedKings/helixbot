# HelixBot

Discord voice client for a self-hosted Helix instance.

## Functionality

- Dockerized deployment.
- Helix instance configured with `HELIX_BASE_URL` in `.env`.
- Persistent Discord-server -> Helix-lobby association in SQLite.
- `/helix join CODE [password]`
- `/helix joinme` joins the invoking user's voice channel and mirrors the linked Helix lobby audio.
- `/helix leaveme` leaves voice while preserving the lobby link.
- Realtime lobby WebSocket subscription for track, pause/resume, seek, and queue-advance changes.
- Helix lobby audio is streamed through FFmpeg into Discord voice using the saved lobby guest token.
- Joining voice mid-track starts near the lobby's current shared playback position.
- Drift and explicit Helix seeks trigger automatic resynchronization.
- Natural Discord track completion reports the specific queue item through Helix's idempotent `/ended` endpoint.
- `/helix status` validates the saved Helix session and shows current lobby playback state.
- `/helix invite` publicly shares the linked lobby's 5-letter code and join URL. Password-protected lobbies are marked as requiring a password, but the password is never included.
- `/helix unlink` deactivates the bot's Helix guest membership and removes the saved association.

Helix remains the source of truth for playback. HelixBot mirrors lobby state; it does not expose Discord commands that play, pause, seek, or skip the Helix lobby.

## Configuration

Copy `.env.example` to `.env` and fill in your Discord bot token:

```env
DISCORD_TOKEN=replace-me
HELIX_BASE_URL=https://helix.unifiedkings.net
HELIXBOT_DB_PATH=/data/helixbot.sqlite3
HELIXBOT_VOLUME=0.10
DISCORD_DEV_GUILD_ID=
LOG_LEVEL=INFO
```

`HELIX_BASE_URL` should point to the Helix instance this bot will use. Self-hosters should replace the example URL with their own Helix URL.

`DISCORD_DEV_GUILD_ID` is useful during development because guild-scoped slash commands update immediately. Leave it blank for normal global command registration.

`HELIXBOT_VOLUME` controls the audio level HelixBot sends to Discord. The default is `0.10` (10%) so the bot does not join a channel at an unexpectedly loud level. Valid values are `0.0` through `1.0`. Individual Discord users can still adjust HelixBot locally on top of this server-side default.

## Discord application setup

Before starting HelixBot, create a Discord application and add the bot to your Discord server.

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and select your HelixBot application.
2. Open **OAuth2** -> **URL Generator**.
3. Under **Scopes**, enable:
   - `bot`
   - `applications.commands`
4. Under **Bot Permissions**, enable at minimum:
   - **View Channels**
   - **Connect**
   - **Speak**
5. If you later want HelixBot to post richer status messages in text channels, you may also grant:
   - **Send Messages**
   - **Embed Links**
6. Copy the generated invite URL at the bottom of the page and open it in your browser.
7. Select the Discord server where HelixBot should be installed and authorize it.

You must have permission to add/manage applications on the Discord server.

HelixBot does **not** need the **Administrator** permission. Granting Administrator is unnecessary and gives the bot significantly broader access than it needs.

## Run

Build and start the bot:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f helixbot
```

The SQLite database is stored in `./data` on the host through the Compose volume.

Once the container is online and Discord has registered the slash commands, type `/helix` in your Discord server to see the available HelixBot commands.
