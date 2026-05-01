# Discord Music Bot (Slash Commands)

A Python Discord music bot that streams audio from YouTube using `yt-dlp` and
FFmpeg. All commands are Discord-native slash commands, with an interactive
now-playing panel that has buttons and dropdowns so you don't have to type
slash commands for everything.

## Commands

| Command | Description |
|---|---|
| `/play <query>` | Search Spotify (with YouTube fallback) or paste a Spotify/YouTube URL or playlist |
| `/skip` | Skip the current song |
| `/pause` / `/resume` | Pause or resume playback |
| `/stop` | Stop playback and clear the queue |
| `/queue` | Show the current queue (visible only to you) |
| `/shuffle` | Randomize the order of the queue |
| `/clearqueue` | Clear the queue without stopping the current song |
| `/qremove <position>` | Remove a song from the queue by its position |
| `/playnext <position>` | Move a queued song to the top of the queue |
| `/nowplaying` | Show the currently playing song |
| `/volume <0-100>` | Set playback volume |
| `/leave` | Disconnect from voice (queue is saved for next `/play`) |

### Admin-only commands
| Command | Description |
|---|---|
| `/music_show_config` | Show current bot security settings |
| `/music_set_channel <#channel>` | Restrict bot use to one channel |
| `/music_set_role <@role>` | Require a role to use the bot |

### The interactive panel

When you `/play` something for the first time, the bot posts a panel in the
channel showing the current song with a progress bar. The panel has:

- **Pause/Play, Skip, Show Queue, Clear Queue** buttons
- **Vol −10 / Vol +10 / Seek** controls
- A **"Play next from queue…"** dropdown (when 2+ songs are queued)
- A **"Remove from queue…"** dropdown (when 1+ songs are queued)

Slash commands give brief private replies (or no reply at all) so the channel
stays clean — the panel is the source of truth.

## File layout

```
bot.py         entrypoint, slash commands, on_ready, shutdown
config.py      env, constants, audit log, spam guard, channel/role permissions
state.py       shared dicts (queues, playing songs, panel messages)
audio.py       yt-dlp + FFmpeg wrappers
spotify.py     Spotify Web API client (URL parsing, search, track lookup)
spotify_auth.py optional OAuth helper for user-scoped Spotify features
panel.py       embed building, view/dropdowns/modal, ticker, inactivity timer
player.py      play_next + advance_and_announce
```

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
3. Click **Reset Token** → **Copy**. Paste this value into `.env`:
   ```
   DISCORD_TOKEN=paste-your-token-here
   ```
   (No privileged intents are required — the bot uses default intents only.)
4. *(Optional)* If you want slash commands to appear instantly in a specific
   server instead of waiting up to an hour for global sync, add:
   ```
   INSTANT_SYNC_GUILD_IDS=123456789012345678
   ```
   Multiple IDs can be comma-separated.
5. In the developer portal, go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Connect`, `Speak`, `Send Messages`, `Use Slash Commands`,
     `Embed Links`
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

Or just double-click **`start_bot.bat`** once the venv is set up.

## 4. Spotify Web App Setup

`/play` uses Spotify as the primary metadata source: paste a Spotify track,
album, or playlist URL and the bot will queue every track; type free text
and the bot looks the song up on Spotify first (better titles than YouTube
search) before resolving it on YouTube for actual playback. If Spotify
isn't configured, `/play` silently falls back to direct YouTube search.

1. In the Spotify Developer Dashboard, create a new app.
2. Add these values to `.env`:
   ```env
   SPOTIFY_CLIENT_ID=your_spotify_client_id
   SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
   ```
   That's all you need — `/play` uses the Client Credentials flow. No
   browser authorization step is required.

### Optional: user-scoped Spotify (for future features)

If you also want to run `spotify_auth.py` (it requests
`user-read/modify-playback-state` scopes for future remote-control
features), add a redirect URI to the app settings (e.g.
`http://localhost:8888/callback`), set:
```env
SPOTIFY_REDIRECT_URI=http://localhost:8888/callback
```
then run:
```powershell
python spotify_auth.py
```
The script writes `SPOTIFY_REFRESH_TOKEN` to `.env`. Use
`python spotify_auth.py --manual` if the local callback can't reach you.

## How it works (plain English)

When you type `/play some song` in a Discord server:

1. The bot joins your voice channel.
2. It searches YouTube for what you typed.
3. It grabs the audio and plays it.
4. It posts a control panel with buttons and dropdowns you can click.

If a song is already playing, your new one gets added to a **queue** — a
waiting list. When the current song ends, the next one plays automatically.

Each Discord server has its own separate queue.

If you `/leave`, the queue is saved in memory so the next `/play` resumes
where you left off. A full bot restart still wipes the queue, but server
security settings (`/music_set_channel`, `/music_set_role`) are saved to
`bot_config.json` and persist.

### The tools doing the real work

- **yt-dlp** — talks to YouTube and finds the audio stream for a video.
- **FFmpeg** — takes that stream and feeds it to Discord.

yt-dlp comes from `pip install`. FFmpeg has to be installed on your computer
(section 1 above).

### Audit log

Every command and button click is logged to `bot_audit.log` next to the bot
script (server, channel, user, action). Useful for figuring out who did what
if something looks off.
