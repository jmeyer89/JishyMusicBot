import asyncio
import time
import discord

from audio import extract_song_info, ffmpeg_options
from config import DEFAULT_VOLUME
from panel import (
    build_panel_embed,
    cancel_inactivity,
    make_controls,
    schedule_inactivity,
    start_now_playing_ticker,
)
from state import (
    announce_channels,
    currently_playing,
    now_playing_messages,
    song_queues,
    volume_levels,
)


async def play_next(guild_id: int, voice_client: discord.VoiceClient) -> None:
    queue = song_queues.get(guild_id, [])
    if not queue:
        currently_playing.pop(guild_id, None)
        schedule_inactivity(guild_id, voice_client.client)
        return
    cancel_inactivity(guild_id)
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
    source = discord.FFmpegPCMAudio(next_song["url"], **ffmpeg_options(seek_to))
    source = discord.PCMVolumeTransformer(source, volume=volume_levels.get(guild_id, DEFAULT_VOLUME))

    def after_playing(error: Exception | None) -> None:
        if error:
            print(f"[player] error during playback in guild {guild_id}: {error}")
        asyncio.run_coroutine_threadsafe(advance_and_announce(guild_id, voice_client), voice_client.client.loop)

    voice_client.play(source, after=after_playing)


async def advance_and_announce(guild_id: int, voice_client: discord.VoiceClient) -> None:
    await play_next(guild_id, voice_client)
    now = currently_playing.get(guild_id)
    if not now:
        return
    panel = now_playing_messages.get(guild_id)
    if panel is not None:
        try:
            await panel.edit(content=None, embed=build_panel_embed(guild_id), view=make_controls(guild_id))
            start_now_playing_ticker(guild_id, now, panel)
            return
        except discord.DiscordException:
            now_playing_messages.pop(guild_id, None)
    channel = announce_channels.get(guild_id)
    if channel is None:
        return
    try:
        sent = await channel.send(embed=build_panel_embed(guild_id), view=make_controls(guild_id))
    except discord.DiscordException:
        return
    now_playing_messages[guild_id] = sent
    start_now_playing_ticker(guild_id, now, sent)
