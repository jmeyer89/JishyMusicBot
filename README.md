# Discord Music Bot (Slash Commands)

A Python Discord music bot that streams audio from YouTube using `yt-dlp` and
FFmpeg. All commands are Discord-native slash commands.

## Commands

| Command | Description |
|---|---|
| `/play <query>` | Search YouTube and play or queue a song |
| `/skip` | Skip the current song |
| `/pause` | Pause playback |
| `/resume` | Resume paused playback |
| `/stop` | Stop playback and clear the queue |
| `/queue` | Show the current queue |
| `/nowplaying` | Show the currently playing song |
| `/volume <0-100>` | Set playback volume |
| `/leave` | Disconnect the bot from voice |

## 1. Install FFmpeg on Windows

FFmpeg must be on your `PATH` for audio playback to work.

**Option A — winget (easiest):**
```powershell
winget install --id=Gyan.FFmpeg -e
```
Then close and reopen your terminal so the new `PATH` entry takes effect.

**Option B — manual:**
1. Download a build from <https://www.gyan.dev/ffmpeg/builds/> (pick
   `ffmpeg-release-essentials.zip`).
2. Extract it to e.g. `C:\ffmpeg`.
3. Add `C:\ffmpeg\bin` to your system `PATH`
   (Start → "Edit the system environment variables" → Environment Variables →
   edit `Path` → New → paste the path).
4. Open a new terminal and verify:
   ```powershell
   ffmpeg -version
   ```

## 2. Get a Discord bot token

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Name it, then open the **Bot** tab and click **Add Bot**.
3. Under **Privileged Gateway Intents**, enable **Server Members Intent** and
   **Message Content Intent** (message content is not required for slash
   commands, but it's useful to have on for future prefix-style additions).
4. Click **Reset Token** → **Copy**. Paste this value into `.env`:
   ```
   DISCORD_TOKEN=paste-your-token-here
   ```
5. Still in the developer portal, go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Connect`, `Speak`, `Send Messages`, `Use Slash Commands`
   - Copy the generated URL, open it in your browser, and invite the bot to
     your server.

## 3. Run the bot

From the project directory:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

On first startup the bot syncs its slash command tree with Discord. Global
sync can take up to an hour to propagate; if you want your commands to appear
instantly in a single test server, swap `await bot.tree.sync()` for
`await bot.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))`.
