"""Pure rendering helpers for the now-playing panel.

This module is the dependency leaf of the `panel` package — it builds embeds
and formats text from the current `state` snapshot, with no side effects.
"""
import time

import discord

from config import DEFAULT_VOLUME
from state import (
    currently_playing,
    queue_expanded,
    search_alternatives,
    song_queues,
    volume_levels,
)


def parse_time(value: str) -> int | None:
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


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def elapsed_seconds(song: dict) -> float:
    started_at = song.get("started_at")
    if started_at is None:
        return 0.0
    paused_total = song.get("paused_total", 0.0)
    paused_at = song.get("paused_at")
    reference = paused_at if paused_at is not None else time.monotonic()
    return reference - started_at - paused_total


def format_now_playing(song: dict) -> str:
    elapsed = elapsed_seconds(song)
    duration = song.get("duration")
    if duration:
        time_str = f" `[{format_time(elapsed)} / {format_time(duration)}]`"
    else:
        time_str = f" `[{format_time(elapsed)}]`"
    return f"**Now playing:** {song['title']}{time_str}"


def format_queue_lines(guild_id: int) -> list[str]:
    pending_songs = song_queues.get(guild_id, [])
    now_playing_song = currently_playing.get(guild_id)
    lines: list[str] = []
    if now_playing_song:
        lines.append(format_now_playing(now_playing_song))
    for index, song in enumerate(pending_songs, start=1):
        lines.append(f"`{index}.` {song['title']} — requested by {song.get('requester', 'unknown')}")
    return lines


def make_progress_bar(elapsed: float, duration: float | None, length: int = 14) -> str:
    if not duration or duration <= 0:
        return "─" * length
    ratio = max(0.0, min(1.0, elapsed / duration))
    knob_pos = int(round(ratio * (length - 1)))
    return "━" * knob_pos + "●" + "─" * (length - 1 - knob_pos)


def build_panel_embed(guild_id: int) -> discord.Embed:
    song = currently_playing.get(guild_id)
    if song is None:
        return discord.Embed(
            title="Nothing playing",
            description="Use `/play` to queue a song.",
            color=discord.Color.dark_gray(),
        )
    paused = song.get("paused_at") is not None
    elapsed = elapsed_seconds(song)
    duration = song.get("duration")
    bar = make_progress_bar(elapsed, duration, length=12)
    elapsed_str = format_time(elapsed)
    if duration:
        description = f"`{elapsed_str} {bar} {format_time(duration)}`"
    else:
        description = f"`{elapsed_str} {bar}`"
    if queue_expanded.get(guild_id):
        pending = song_queues.get(guild_id, [])
        if not pending:
            description += "\n\n**Up next:**\n*Queue is empty.*"
        else:
            entries = list(reversed(list(enumerate(pending, start=1))))
            lines: list[str] = []
            for i, s in entries:
                link = s.get("webpage_url") or song.get("webpage_url") or ""
                title_text = (s.get("title") or "Unknown title").replace("[", "(").replace("]", ")")
                if len(title_text) > 50:
                    title_text = title_text[:47] + "…"
                lines.append(f"`{i:>2}.` [{title_text}]({link})")
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


def panel_fingerprint(guild_id: int) -> tuple:
    song = currently_playing.get(guild_id)
    if song is None:
        return ("none",)
    paused = song.get("paused_at") is not None
    # Bucket elapsed in 10s increments so the ticker only forces redraws when the
    # displayed time actually moves a meaningful amount.
    elapsed_bucket = int(elapsed_seconds(song) // 10)
    expanded = queue_expanded.get(guild_id, False)
    queue_ids: tuple = ()
    if expanded:
        queue_ids = tuple(s.get("queue_id", "") for s in song_queues.get(guild_id, []))
    volume_pct = int(round(volume_levels.get(guild_id, DEFAULT_VOLUME) * 100))
    has_alts = bool(search_alternatives.get(guild_id))
    return (song.get("title"), elapsed_bucket, paused, expanded, queue_ids, volume_pct, has_alts)
