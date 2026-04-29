import os
import asyncio
import json
import logging
import random
import time
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
INSTANT_SYNC_GUILD_IDS = [205409317283823627, 708832660444938244]

intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)

song_queues: dict[int, list[dict]] = {}
currently_playing: dict[int, dict] = {}
announce_channels: dict[int, discord.abc.Messageable] = {}
now_playing_tasks: dict[int, asyncio.Task] = {}
now_playing_messages: dict[int, discord.Message] = {}
saved_queues: dict[int, list[dict]] = {}

MAX_QUEUE_LENGTH = 30
NOW_PLAYING_REFRESH_SECONDS = 1
DEFAULT_VOLUME = 0.5

queue_expanded: dict[int, bool] = {}
volume_levels: dict[int, float] = {}

_CONFIG_FILE = Path(__file__).parent / "bot_config.json"
_AUDIT_LOG_FILE = Path(__file__).parent / "bot_audit.log"
_SETUP_COMMANDS = {"music_show_config", "music_set_channel", "music_set_role"}

guild_config: dict[int, dict] = {}


def _load_config() -> None:
    global guild_config
    if not _CONFIG_FILE.exists():
        guild_config = {}
        return
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        guild_config = {int(k): v for k, v in data.items()}
    except Exception as load_error:
        print(f"[config] load failed: {load_error}")
        guild_config = {}


def _save_config() -> None:
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in guild_config.items()}, f, indent=2)
    except Exception as save_error:
        print(f"[config] save failed: {save_error}")


_audit_logger = logging.getLogger("jishybot.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
if not _audit_logger.handlers:
    _audit_handler = logging.FileHandler(_AUDIT_LOG_FILE, encoding="utf-8")
    _audit_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _audit_logger.addHandler(_audit_handler)


def _audit(interaction: discord.Interaction, action: str) -> None:
    try:
        guild_name = interaction.guild.name if interaction.guild else "DM"
        channel_name = getattr(interaction.channel, "name", None) or str(interaction.channel_id)
        user = f"{interaction.user}({interaction.user.id})"
        _audit_logger.info(f"{guild_name} #{channel_name} | {user} | {action}")
    except Exception:
        pass


def _is_interaction_allowed(interaction: discord.Interaction) -> tuple[bool, str | None]:
    if interaction.guild is None:
        return False, "This bot only works in servers, not in DMs."
    cfg = guild_config.get(interaction.guild.id, {})
    allowed_channels = cfg.get("allowed_channels") or []
    if allowed_channels and interaction.channel_id not in allowed_channels:
        return False, "This bot is restricted to a different channel."
    allowed_role = cfg.get("allowed_role")
    if allowed_role:
        member = interaction.user
        if not isinstance(member, discord.Member) or not any(r.id == allowed_role for r in member.roles):
            return False, "You don't have the role required to use this bot."
    return True, None


async def _slash_interaction_check(interaction: discord.Interaction) -> bool:
    try:
        cmd_name = interaction.command.name if interaction.command else None
        if cmd_name in _SETUP_COMMANDS and interaction.guild is not None:
            member = interaction.user
            if isinstance(member, discord.Member) and member.guild_permissions.administrator:
                _audit(interaction, f"/{cmd_name}")
                return True
        allowed, reason = _is_interaction_allowed(interaction)
        if not allowed:
            try:
                await interaction.response.send_message(
                    reason or "This bot is restricted here.", ephemeral=True
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return False
        _audit(interaction, f"/{cmd_name or 'unknown'}")
        return True
    except Exception as check_error:
        print(f"[security] slash check error: {check_error}")
        return False

YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "skip_download": True,
    "extractor_args": {"youtube": {"player_client": ["android", "tv", "web"]}},
}

def _ffmpeg_options(seek: float | None = None) -> dict:
    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if seek and seek > 0:
        before = f"-ss {seek:.3f} {before}"
    return {
        "before_options": before,
        "options": "-vn -af dynaudnorm",
    }

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

YTDL_PLAYLIST_OPTIONS = {
    **YTDL_FORMAT_OPTIONS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
}
ytdl_playlist = yt_dlp.YoutubeDL(YTDL_PLAYLIST_OPTIONS)


def _is_playlist_url(query: str) -> bool:
    return "playlist?list=" in query


async def extract_playlist_info(query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(query, download=False))
    entries = data.get("entries") or []
    songs: list[dict] = []
    for entry in entries:
        if not entry:
            continue
        webpage_url = entry.get("url") or entry.get("webpage_url")
        if not webpage_url:
            video_id = entry.get("id")
            if video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        if not webpage_url:
            continue
        thumbs = entry.get("thumbnails") or []
        songs.append({
            "title": entry.get("title", "Unknown title"),
            "url": None,
            "webpage_url": webpage_url,
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None),
        })
    return songs


async def extract_song_info(query: str) -> dict:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return {
        "title": data.get("title", "Unknown title"),
        "url": data["url"],
        "webpage_url": data.get("webpage_url", query),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
    }


async def play_next(guild_id: int, voice_client: discord.VoiceClient) -> None:
    queue = song_queues.get(guild_id, [])
    if not queue:
        currently_playing.pop(guild_id, None)
        return
    next_song = queue.pop(0)
    if not next_song.get("url"):
        source_query = next_song.get("webpage_url") or next_song.get("title")
        if not source_query:
            await play_next(guild_id, voice_client)
            return
        try:
            fresh = await extract_song_info(source_query)
        except Exception as extraction_error:
            print(f"[player] could not refresh URL for {next_song.get('title', source_query)}: {extraction_error}")
            await play_next(guild_id, voice_client)
            return
        next_song["url"] = fresh["url"]
        if not next_song.get("title"):
            next_song["title"] = fresh.get("title")
        if not next_song.get("duration"):
            next_song["duration"] = fresh.get("duration")
        if not next_song.get("webpage_url"):
            next_song["webpage_url"] = fresh.get("webpage_url")
        if not next_song.get("thumbnail"):
            next_song["thumbnail"] = fresh.get("thumbnail")
    if not voice_client.is_connected():
        return
    seek_to = float(next_song.pop("seek_to", 0.0) or 0.0)
    next_song["started_at"] = time.monotonic() - seek_to
    next_song["paused_total"] = 0.0
    next_song["paused_at"] = None
    currently_playing[guild_id] = next_song
    source = discord.FFmpegPCMAudio(next_song["url"], **_ffmpeg_options(seek_to))
    source = discord.PCMVolumeTransformer(source, volume=volume_levels.get(guild_id, DEFAULT_VOLUME))
    def after_playing(error: Exception | None) -> None:
        if error:
            print(f"[player] error during playback in guild {guild_id}: {error}")
        asyncio.run_coroutine_threadsafe(_advance_and_announce(guild_id, voice_client), bot.loop)
    voice_client.play(source, after=after_playing)


async def _advance_and_announce(guild_id: int, voice_client: discord.VoiceClient) -> None:
    await play_next(guild_id, voice_client)
    now = currently_playing.get(guild_id)
    if not now:
        return
    panel = now_playing_messages.get(guild_id)
    if panel is not None:
        try:
            await panel.edit(content=None, embed=_build_panel_embed(guild_id), view=_make_controls(guild_id))
            _start_now_playing_ticker(guild_id, now, panel)
            return
        except discord.DiscordException:
            now_playing_messages.pop(guild_id, None)
    channel = announce_channels.get(guild_id)
    if channel is None:
        return
    try:
        sent = await channel.send(embed=_build_panel_embed(guild_id), view=_make_controls(guild_id))
    except discord.DiscordException:
        return
    now_playing_messages[guild_id] = sent
    _start_now_playing_ticker(guild_id, now, sent)


def _parse_time(value: str) -> int | None:
    parts = value.strip().split(":")
    if not parts or any(not p.strip() for p in parts):
        return None
    try:
        if len(parts) == 1:
            return int(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


def _format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _elapsed_seconds(song: dict) -> float:
    started_at = song.get("started_at")
    if started_at is None:
        return 0.0
    paused_total = song.get("paused_total", 0.0)
    paused_at = song.get("paused_at")
    reference = paused_at if paused_at is not None else time.monotonic()
    return reference - started_at - paused_total


def _format_now_playing(song: dict) -> str:
    elapsed = _elapsed_seconds(song)
    duration = song.get("duration")
    if duration:
        time_str = f" `[{_format_time(elapsed)} / {_format_time(duration)}]`"
    else:
        time_str = f" `[{_format_time(elapsed)}]`"
    return f"**Now playing:** {song['title']}{time_str}"


async def _tick_now_playing(guild_id: int, song: dict, message) -> None:
    last_fingerprint: tuple | None = None
    try:
        while True:
            await asyncio.sleep(NOW_PLAYING_REFRESH_SECONDS)
            if currently_playing.get(guild_id) is not song:
                return
            fingerprint = _panel_fingerprint(guild_id)
            if fingerprint == last_fingerprint:
                continue
            try:
                await message.edit(content=None, embed=_build_panel_embed(guild_id))
                last_fingerprint = fingerprint
            except discord.DiscordException:
                return
    except asyncio.CancelledError:
        return
    finally:
        if now_playing_tasks.get(guild_id) is asyncio.current_task():
            now_playing_tasks.pop(guild_id, None)


def _start_now_playing_ticker(guild_id: int, song: dict, message) -> None:
    existing = now_playing_tasks.get(guild_id)
    if existing is not None and not existing.done():
        existing.cancel()
    now_playing_tasks[guild_id] = asyncio.create_task(_tick_now_playing(guild_id, song, message))


def _format_queue_lines(guild_id: int) -> list[str]:
    pending_songs = song_queues.get(guild_id, [])
    now_playing_song = currently_playing.get(guild_id)
    lines: list[str] = []
    if now_playing_song:
        lines.append(_format_now_playing(now_playing_song))
    for index, song in enumerate(pending_songs, start=1):
        lines.append(f"`{index}.` {song['title']} — requested by {song.get('requester', 'unknown')}")
    return lines


def _make_progress_bar(elapsed: float, duration: float | None, length: int = 14) -> str:
    if not duration or duration <= 0:
        return "─" * length
    ratio = max(0.0, min(1.0, elapsed / duration))
    knob_pos = int(round(ratio * (length - 1)))
    return "━" * knob_pos + "●" + "─" * (length - 1 - knob_pos)


def _build_panel_embed(guild_id: int) -> discord.Embed:
    song = currently_playing.get(guild_id)
    if song is None:
        return discord.Embed(
            title="Nothing playing",
            description="Use `/play` to queue a song.",
            color=discord.Color.dark_gray(),
        )
    paused = song.get("paused_at") is not None
    elapsed = _elapsed_seconds(song)
    duration = song.get("duration")
    bar = _make_progress_bar(elapsed, duration, length=12)
    elapsed_str = _format_time(elapsed)
    if duration:
        description = f"`{elapsed_str} {bar} {_format_time(duration)}`"
    else:
        description = f"`{elapsed_str} {bar}`"
    if queue_expanded.get(guild_id):
        pending = song_queues.get(guild_id, [])
        if not pending:
            description += "\n\n**Up next:**\n*Queue is empty.*"
        else:
            entries = list(reversed(list(enumerate(pending, start=1))))
            lines: list[str] = []
            running = len(description) + len("\n\n**Up next:**\n")
            for i, s in entries:
                link = s.get("webpage_url") or song.get("webpage_url") or ""
                title_text = (s.get("title") or "Unknown title").replace("[", "(").replace("]", ")")
                if len(title_text) > 70:
                    title_text = title_text[:67] + "…"
                line = f"`{i}.` [{title_text}]({link}) — {s.get('requester', 'unknown')}"
                if running + len(line) + 1 > 3900:
                    remaining = len(entries) - len(lines)
                    lines.append(f"…and {remaining} more")
                    break
                lines.append(line)
                running += len(line) + 1
            description += "\n\n**Up next:**\n" + "\n".join(lines)
    embed = discord.Embed(
        title=song.get("title", "Unknown title"),
        url=song.get("webpage_url"),
        description=description,
        color=discord.Color.gold() if paused else discord.Color.blurple(),
    )
    embed.set_author(name="Paused" if paused else "Now playing")
    requester = song.get("requester")
    if requester:
        embed.set_footer(text=f"Requested by {requester}")
    thumb = song.get("thumbnail")
    if thumb:
        embed.set_thumbnail(url=thumb)
    return embed


def _panel_fingerprint(guild_id: int) -> tuple:
    song = currently_playing.get(guild_id)
    if song is None:
        return ("none",)
    paused = song.get("paused_at") is not None
    elapsed_str = _format_time(_elapsed_seconds(song))
    expanded = queue_expanded.get(guild_id, False)
    queue_titles: tuple = ()
    if expanded:
        queue_titles = tuple(s.get("title", "") for s in song_queues.get(guild_id, []))
    return (song.get("title"), elapsed_str, paused, expanded, queue_titles)


def _make_controls(guild_id: int) -> "QueueControlsView":
    view = QueueControlsView()
    if queue_expanded.get(guild_id):
        view.show_queue.label = "Hide Queue"
    percent = int(round(volume_levels.get(guild_id, DEFAULT_VOLUME) * 100))
    view.vol_display.label = f"{percent}%"
    if len(song_queues.get(guild_id, [])) > 1:
        view.add_item(QueuePlaySelect(guild_id))
    return view


class QueuePlaySelect(discord.ui.Select):
    def __init__(self, guild_id: int) -> None:
        pending = song_queues.get(guild_id, [])
        options: list[discord.SelectOption] = []
        for i in range(len(pending) - 1, -1, -1):
            song = pending[i]
            title = (song.get("title") or "Unknown title")[:95]
            options.append(discord.SelectOption(label=f"{i + 1}. {title}", value=str(i)))
            if len(options) >= 25:
                break
        super().__init__(
            placeholder="Play next from queue…",
            options=options,
            min_values=1,
            max_values=1,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        try:
            idx = int(self.values[0])
        except (ValueError, IndexError):
            await interaction.response.defer()
            return
        queue = song_queues.get(guild_id, [])
        if idx <= 0 or idx >= len(queue):
            await interaction.response.defer()
            return
        song = queue.pop(idx)
        queue.insert(0, song)
        await interaction.response.edit_message(view=_make_controls(guild_id))


class SeekModal(discord.ui.Modal, title="Seek"):
    position = discord.ui.TextInput(
        label="Time (mm:ss or seconds)",
        placeholder="1:30",
        required=True,
        max_length=8,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        seconds = _parse_time(self.position.value)
        guild_id = interaction.guild.id
        song = currently_playing.get(guild_id)
        voice_client = interaction.guild.voice_client
        if seconds is None or song is None or voice_client is None:
            await interaction.response.defer()
            return
        duration = song.get("duration")
        if duration and seconds >= duration:
            await interaction.response.defer()
            return
        seek_seconds = max(0, seconds)
        new_entry = {
            "title": song.get("title"),
            "url": None,
            "webpage_url": song.get("webpage_url"),
            "duration": song.get("duration"),
            "thumbnail": song.get("thumbnail"),
            "requester": song.get("requester"),
            "seek_to": float(seek_seconds),
        }
        song_queues.setdefault(guild_id, []).insert(0, new_entry)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await interaction.response.defer()


class QueueControlsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed, reason = _is_interaction_allowed(interaction)
        if not allowed:
            try:
                await interaction.response.send_message(
                    reason or "This bot is restricted here.", ephemeral=True
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return False
        custom_id = (interaction.data or {}).get("custom_id") or "unknown"
        _audit(interaction, f"component:{custom_id}")
        return True

    @discord.ui.button(label="Pause/Play", style=discord.ButtonStyle.success)
    async def pause_play_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        if voice_client.is_playing():
            voice_client.pause()
            song = currently_playing.get(interaction.guild.id)
            if song is not None and song.get("paused_at") is None:
                song["paused_at"] = time.monotonic()
            await interaction.response.defer()
            return
        if voice_client.is_paused():
            voice_client.resume()
            song = currently_playing.get(interaction.guild.id)
            if song is not None and song.get("paused_at") is not None:
                song["paused_total"] = song.get("paused_total", 0.0) + (time.monotonic() - song["paused_at"])
                song["paused_at"] = None
            await interaction.response.defer()
            return
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Show Queue", style=discord.ButtonStyle.secondary)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        panel = now_playing_messages.get(guild_id)
        if panel is None or interaction.message.id != panel.id:
            lines = _format_queue_lines(guild_id)
            if not lines:
                await interaction.response.send_message("The queue is empty.", ephemeral=True)
                return
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return
        queue_expanded[guild_id] = not queue_expanded.get(guild_id, False)
        button.label = "Hide Queue" if queue_expanded[guild_id] else "Show Queue"
        await interaction.response.edit_message(
            content=None, embed=_build_panel_embed(guild_id), view=self
        )

    @discord.ui.button(label="Clear Queue", style=discord.ButtonStyle.danger)
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        pending_count = len(song_queues.get(guild_id, []))
        song_queues[guild_id] = []
        saved_queues.pop(guild_id, None)
        if pending_count == 0:
            await interaction.response.send_message("The queue is already empty.", ephemeral=True)
            return
        panel = now_playing_messages.get(guild_id)
        if panel is not None and interaction.message.id == panel.id:
            await interaction.response.edit_message(
                content=None, embed=_build_panel_embed(guild_id), view=self
            )
            return
        await interaction.response.send_message(
            f"Cleared {pending_count} song(s) from the queue.", ephemeral=True
        )

    @discord.ui.button(label="Vol −10", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        current = volume_levels.get(guild_id, DEFAULT_VOLUME)
        new_level = max(0.0, round(current - 0.10, 2))
        volume_levels[guild_id] = new_level
        voice_client = interaction.guild.voice_client
        if voice_client is not None and voice_client.source is not None:
            voice_client.source.volume = new_level
        await interaction.response.edit_message(view=_make_controls(guild_id))

    @discord.ui.button(label="100%", style=discord.ButtonStyle.secondary, disabled=True, row=1)
    async def vol_display(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        return

    @discord.ui.button(label="Vol +10", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        current = volume_levels.get(guild_id, DEFAULT_VOLUME)
        new_level = min(1.0, round(current + 0.10, 2))
        volume_levels[guild_id] = new_level
        voice_client = interaction.guild.voice_client
        if voice_client is not None and voice_client.source is not None:
            voice_client.source.volume = new_level
        await interaction.response.edit_message(view=_make_controls(guild_id))

    @discord.ui.button(label="Seek", style=discord.ButtonStyle.primary, row=1)
    async def seek_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SeekModal())


@bot.event
async def on_ready() -> None:
    try:
        for guild_id in INSTANT_SYNC_GUILD_IDS:
            guild = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {guild_id}.")
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared global commands.")
    except Exception as sync_error:
        print(f"Failed to sync command tree: {sync_error}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="play", description="Search YouTube and play or queue a song.")
@app_commands.describe(query="A YouTube URL or a search term")
async def play(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    user_voice_state = interaction.user.voice
    if user_voice_state is None or user_voice_state.channel is None:
        await interaction.followup.send("You need to be in a voice channel first.", ephemeral=True)
        return
    target_channel = user_voice_state.channel
    guild_id = interaction.guild.id
    saved = saved_queues.pop(guild_id, None)
    if saved:
        song_queues.setdefault(guild_id, []).extend(saved)
    current_queue_length = len(song_queues.get(guild_id, []))
    if current_queue_length >= MAX_QUEUE_LENGTH:
        await interaction.followup.send(
            f"The queue is full ({MAX_QUEUE_LENGTH} songs max). Wait for some to finish or use /stop to clear it.",
            ephemeral=True,
        )
        return
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await target_channel.connect()
    elif voice_client.channel != target_channel:
        await voice_client.move_to(target_channel)
    try:
        if _is_playlist_url(query):
            songs = await extract_playlist_info(query)
            if not songs:
                await interaction.followup.send("That playlist is empty or unavailable.")
                return
        else:
            songs = [await extract_song_info(query)]
    except Exception as extraction_error:
        await interaction.followup.send(f"Could not fetch that: {extraction_error}")
        return
    remaining_slots = MAX_QUEUE_LENGTH - current_queue_length
    to_add = songs[:remaining_slots]
    skipped = len(songs) - len(to_add)
    for queued_song in to_add:
        queued_song["requester"] = interaction.user.display_name
    song_queues.setdefault(guild_id, []).extend(to_add)
    announce_channels[guild_id] = interaction.channel
    if not voice_client.is_playing() and not voice_client.is_paused():
        await play_next(guild_id, voice_client)
        started = currently_playing.get(guild_id, to_add[0])
        prefix = ""
        if len(to_add) > 1:
            prefix = f"Queued {len(to_add)} songs from playlist"
            if skipped:
                prefix += f" ({skipped} skipped — queue cap is {MAX_QUEUE_LENGTH})"
            prefix += ".\n"
        queue_expanded.pop(guild_id, None)
        sent = await interaction.followup.send(
            content=prefix.rstrip() if prefix else None,
            embed=_build_panel_embed(guild_id),
            view=_make_controls(guild_id),
        )
        now_playing_messages[guild_id] = sent
        _start_now_playing_ticker(guild_id, started, sent)
    else:
        if len(to_add) == 1:
            position_in_queue = len(song_queues[guild_id])
            message = f"Queued **{to_add[0]['title']}** at position {position_in_queue}."
        else:
            message = f"Queued {len(to_add)} songs from playlist."
            if skipped:
                message += f" ({skipped} skipped — queue cap is {MAX_QUEUE_LENGTH})"
        now_playing_song = currently_playing.get(guild_id)
        if now_playing_song:
            message += f"\n{_format_now_playing(now_playing_song)}"
        await interaction.followup.send(message, view=QueueControlsView())


@bot.tree.command(name="skip", description="Skip the current song.")
async def skip(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    voice_client.stop()
    await interaction.response.send_message("Skipped.")


@bot.tree.command(name="pause", description="Pause playback.")
async def pause(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing to pause.", ephemeral=True)
        return
    voice_client.pause()
    song = currently_playing.get(interaction.guild.id)
    if song is not None and song.get("paused_at") is None:
        song["paused_at"] = time.monotonic()
    await interaction.response.send_message("Paused.")


@bot.tree.command(name="resume", description="Resume paused playback.")
async def resume(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_paused():
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    voice_client.resume()
    song = currently_playing.get(interaction.guild.id)
    if song is not None and song.get("paused_at") is not None:
        song["paused_total"] = song.get("paused_total", 0.0) + (time.monotonic() - song["paused_at"])
        song["paused_at"] = None
    await interaction.response.send_message("Resumed.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    song_queues[guild_id] = []
    currently_playing.pop(guild_id, None)
    now_playing_messages.pop(guild_id, None)
    saved_queues.pop(guild_id, None)
    queue_expanded.pop(guild_id, None)
    existing_task = now_playing_tasks.pop(guild_id, None)
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
    voice_client = interaction.guild.voice_client
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current song queue.")
async def queue_command(interaction: discord.Interaction) -> None:
    lines = _format_queue_lines(interaction.guild.id)
    if not lines:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    await interaction.response.send_message("\n".join(lines), view=QueueControlsView())


@bot.tree.command(name="shuffle", description="Shuffle the current queue.")
async def shuffle(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    queue = song_queues.get(guild_id, [])
    if len(queue) < 2:
        await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        return
    random.shuffle(queue)
    await interaction.response.send_message(f"Shuffled {len(queue)} songs.")


@bot.tree.command(name="clearqueue", description="Clear the queue without stopping the current song.")
async def clearqueue(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    pending_count = len(song_queues.get(guild_id, []))
    song_queues[guild_id] = []
    saved_queues.pop(guild_id, None)
    if pending_count == 0:
        await interaction.response.send_message("The queue is already empty.", ephemeral=True)
        return
    await interaction.response.send_message(f"Cleared {pending_count} song(s) from the queue.")


@bot.tree.command(name="qremove", description="Remove a song from the queue by its position.")
@app_commands.describe(position="Queue position to remove (see /queue)")
async def qremove(interaction: discord.Interaction, position: int) -> None:
    guild_id = interaction.guild.id
    queue = song_queues.get(guild_id, [])
    if not queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    if position < 1 or position > len(queue):
        await interaction.response.send_message(
            f"Position must be between 1 and {len(queue)}.", ephemeral=True
        )
        return
    removed = queue.pop(position - 1)
    await interaction.response.send_message(f"Removed **{removed['title']}** from the queue.")


@bot.tree.command(name="playnext", description="Move a queued song to the top of the queue.")
@app_commands.describe(position="Queue position to move to the top (see /queue)")
async def playnext(interaction: discord.Interaction, position: int) -> None:
    guild_id = interaction.guild.id
    queue = song_queues.get(guild_id, [])
    if not queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    if position < 1 or position > len(queue):
        await interaction.response.send_message(
            f"Position must be between 1 and {len(queue)}.", ephemeral=True
        )
        return
    if position == 1:
        await interaction.response.send_message(
            f"**{queue[0]['title']}** is already next.", ephemeral=True
        )
        return
    song = queue.pop(position - 1)
    queue.insert(0, song)
    await interaction.response.send_message(f"Moved **{song['title']}** to the top of the queue.")


@bot.tree.command(name="nowplaying", description="Show the currently playing song.")
async def nowplaying(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    now_playing_song = currently_playing.get(guild_id)
    if now_playing_song is None:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{_format_now_playing(now_playing_song)}\n{now_playing_song.get('webpage_url', '')}"
    )


@bot.tree.command(name="volume", description="Set playback volume (0-100).")
@app_commands.describe(level="Volume level from 0 to 100")
async def volume(interaction: discord.Interaction, level: int) -> None:
    if level < 0 or level > 100:
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    volume_levels[guild_id] = level / 100
    voice_client = interaction.guild.voice_client
    if voice_client is not None and voice_client.source is not None:
        voice_client.source.volume = level / 100
    panel = now_playing_messages.get(guild_id)
    if panel is not None:
        try:
            await panel.edit(view=_make_controls(guild_id))
        except discord.DiscordException:
            pass
    await interaction.response.send_message(f"Volume set to {level}%.")


@bot.tree.command(name="leave", description="Disconnect the bot from voice.")
async def leave(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    pending = list(song_queues.get(guild_id, []))
    interrupted = currently_playing.get(guild_id)
    if interrupted is not None:
        pending.insert(0, interrupted)
    for queued_song in pending:
        queued_song.pop("url", None)
        queued_song.pop("started_at", None)
        queued_song.pop("paused_total", None)
        queued_song.pop("paused_at", None)
    if pending:
        saved_queues[guild_id] = pending
    song_queues.pop(guild_id, None)
    currently_playing.pop(guild_id, None)
    announce_channels.pop(guild_id, None)
    now_playing_messages.pop(guild_id, None)
    queue_expanded.pop(guild_id, None)
    existing_task = now_playing_tasks.pop(guild_id, None)
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
    await voice_client.disconnect()
    if pending:
        await interaction.response.send_message(
            f"Disconnected. {len(pending)} song(s) saved — use /play to resume."
        )
    else:
        await interaction.response.send_message("Disconnected.")


@bot.tree.command(name="music_show_config", description="Show current bot security settings.")
@app_commands.default_permissions(administrator=True)
async def music_show_config(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        return
    cfg = guild_config.get(interaction.guild.id, {})
    allowed_channels = cfg.get("allowed_channels") or []
    allowed_role = cfg.get("allowed_role")
    lines = ["**Bot security settings:**"]
    if allowed_channels:
        chs = ", ".join(f"<#{cid}>" for cid in allowed_channels)
        lines.append(f"Allowed channels: {chs}")
    else:
        lines.append("Allowed channels: *all channels*")
    if allowed_role:
        lines.append(f"Required role: <@&{allowed_role}>")
    else:
        lines.append("Required role: *none (everyone allowed)*")
    lines.append("DMs: blocked")
    lines.append(f"Audit log: `{_AUDIT_LOG_FILE.name}` (on disk where the bot runs)")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="music_set_channel", description="Restrict the bot to one channel (admin only).")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel to restrict to. Leave blank to clear restriction.")
async def music_set_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
) -> None:
    if interaction.guild is None:
        return
    cfg = guild_config.setdefault(interaction.guild.id, {})
    if channel is None:
        cfg.pop("allowed_channels", None)
        msg = "Channel restriction cleared. The bot now works in all channels."
    else:
        cfg["allowed_channels"] = [channel.id]
        msg = f"The bot is now restricted to {channel.mention}."
    _save_config()
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="music_set_role", description="Require a role to use the bot (admin only).")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role="Role required. Leave blank to clear requirement.")
async def music_set_role(
    interaction: discord.Interaction,
    role: discord.Role | None = None,
) -> None:
    if interaction.guild is None:
        return
    cfg = guild_config.setdefault(interaction.guild.id, {})
    if role is None:
        cfg.pop("allowed_role", None)
        msg = "Role requirement cleared. All members can use the bot."
    else:
        cfg["allowed_role"] = role.id
        msg = f"The bot now requires the {role.mention} role."
    _save_config()
    await interaction.response.send_message(msg, ephemeral=True)


bot.tree.interaction_check = _slash_interaction_check
_load_config()


_original_close = bot.close


async def _close_with_voice_cleanup() -> None:
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=False)
        except Exception as disconnect_error:
            print(f"[shutdown] error disconnecting voice: {disconnect_error}")
    await _original_close()


bot.close = _close_with_voice_cleanup


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(DISCORD_TOKEN)
