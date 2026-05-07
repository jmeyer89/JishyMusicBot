import asyncio
import time
import discord

from audio import extract_song_info, ffmpeg_options
from config import DEFAULT_VOLUME
from panel import (
    cancel_inactivity,
    ensure_panel_writer,
    request_panel_update,
    schedule_inactivity,
    start_now_playing_ticker,
)
from state import (
    currently_playing,
    now_playing_messages,
    play_locks,
    song_queues,
    volume_levels,
)


async def play_next(guild_id: int, voice_client: discord.VoiceClient) -> bool:
    """Start the next queued song. Returns True iff voice_client.play() was called."""
    lock = play_locks.setdefault(guild_id, asyncio.Lock())
    while True:
        async with lock:
            if voice_client.is_playing() or voice_client.is_paused():
                return False
            if not voice_client.is_connected():
                return False
            queue = song_queues.get(guild_id, [])
            if not queue:
                currently_playing.pop(guild_id, None)
                schedule_inactivity(guild_id, voice_client.client)
                return False
            cancel_inactivity(guild_id)
            next_song = queue.pop(0)
            if not next_song.get("url"):
                source_query = (
                    next_song.get("search_query")
                    or next_song.get("webpage_url")
                    or next_song.get("title")
                )
                if not source_query:
                    continue
                try:
                    fresh = await extract_song_info(
                        source_query,
                        expected_duration=next_song.get("duration"),
                    )
                except Exception as extraction_error:
                    print(f"[player] could not refresh URL for {next_song.get('title', source_query)}: {extraction_error}")
                    continue
                next_song["url"] = fresh["url"]
                if not next_song.get("title"):
                    next_song["title"] = fresh.get("title")
                if not next_song.get("duration"):
                    next_song["duration"] = fresh.get("duration")
                if not next_song.get("webpage_url"):
                    next_song["webpage_url"] = fresh.get("webpage_url")
                if not next_song.get("thumbnail"):
                    next_song["thumbnail"] = fresh.get("thumbnail")
                if voice_client.is_playing() or voice_client.is_paused():
                    # Race: another play_next started while we were resolving.
                    song_queues.setdefault(guild_id, []).insert(0, next_song)
                    return False
                if not voice_client.is_connected():
                    return False
            seek_to = float(next_song.pop("seek_to", 0.0) or 0.0)
            next_song["started_at"] = time.monotonic() - seek_to
            next_song["paused_total"] = 0.0
            next_song["paused_at"] = None
            currently_playing[guild_id] = next_song
            source = discord.FFmpegPCMAudio(next_song["url"], **ffmpeg_options(seek_to))
            source = discord.PCMVolumeTransformer(source, volume=volume_levels.get(guild_id, DEFAULT_VOLUME))

            def after_playing(error: Exception | None) -> None:
                if error:
                    print(f"[player] error during playback in guild {guild_id}: {error}")
                asyncio.run_coroutine_threadsafe(advance_and_announce(guild_id, voice_client), voice_client.client.loop)

            try:
                voice_client.play(source, after=after_playing)
            except discord.ClientException as play_error:
                print(f"[player] voice_client.play race in guild {guild_id}: {play_error}")
                song_queues.setdefault(guild_id, []).insert(0, next_song)
                return False
            return True


async def advance_and_announce(guild_id: int, voice_client: discord.VoiceClient) -> None:
    await play_next(guild_id, voice_client)
    now = currently_playing.get(guild_id)
    if not now:
        return
    if now_playing_messages.get(guild_id) is None:
        # Panel was deleted by the user; don't repost. Next /play will create one.
        return
    ensure_panel_writer(guild_id)
    request_panel_update(guild_id)
    start_now_playing_ticker(guild_id, now)
