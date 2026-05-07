"""Coalescing per-guild panel writer + the now-playing ticker.

UI handlers, lifecycle events, and the ticker all funnel through
`request_panel_update(guild_id)`. A single `_panel_writer` task per guild
collapses bursts of update requests into the latest desired state, paces
edits to stay under Discord's per-message PATCH bucket, and skips redundant
no-op edits via `panel_fingerprint`.
"""
import asyncio
import time

import discord

from config import NOW_PLAYING_REFRESH_SECONDS, PANEL_MIN_EDIT_INTERVAL
from state import (
    currently_playing,
    now_playing_messages,
    now_playing_tasks,
    panel_last_edit_at,
    panel_update_events,
    panel_writer_tasks,
)
from panel.render import build_panel_embed, panel_fingerprint


def request_panel_update(guild_id: int) -> None:
    """Signal the per-guild writer that the panel needs a redraw. Idempotent and cheap."""
    event = panel_update_events.get(guild_id)
    if event is not None:
        event.set()


async def _panel_writer(guild_id: int) -> None:
    """Single coalescing writer per guild: collapses bursts of update requests
    into the latest desired state and paces edits to stay under Discord's
    per-message PATCH bucket."""
    # Lazy import: panel.view imports panel.writer at module load.
    from panel.view import make_controls

    event = panel_update_events.get(guild_id)
    if event is None:
        return
    last_fingerprint: tuple | None = None
    try:
        while True:
            await event.wait()
            event.clear()
            # Pace: hold off if we sent something very recently. New requests
            # arriving during the sleep just re-set the event for the next loop.
            now = time.monotonic()
            since_last = now - panel_last_edit_at.get(guild_id, 0.0)
            if since_last < PANEL_MIN_EDIT_INTERVAL:
                await asyncio.sleep(PANEL_MIN_EDIT_INTERVAL - since_last)
                event.clear()
            message = now_playing_messages.get(guild_id)
            if message is None:
                continue
            fingerprint = panel_fingerprint(guild_id)
            if fingerprint == last_fingerprint:
                continue
            try:
                await message.edit(
                    content=None,
                    embed=build_panel_embed(guild_id),
                    view=make_controls(guild_id),
                )
                panel_last_edit_at[guild_id] = time.monotonic()
                last_fingerprint = fingerprint
            except discord.NotFound:
                now_playing_messages.pop(guild_id, None)
            except discord.HTTPException as edit_error:
                # 401 (50027) means a webhook-message token expired — that
                # message is permanently un-editable; drop it and stop retrying.
                if edit_error.status == 401:
                    print(f"[_panel_writer] panel token expired; dropping panel: {edit_error}")
                    now_playing_messages.pop(guild_id, None)
                else:
                    print(f"[_panel_writer] edit failed: {type(edit_error).__name__}: {edit_error}")
    except asyncio.CancelledError:
        return
    finally:
        if panel_writer_tasks.get(guild_id) is asyncio.current_task():
            panel_writer_tasks.pop(guild_id, None)
            panel_update_events.pop(guild_id, None)


def ensure_panel_writer(guild_id: int) -> None:
    existing = panel_writer_tasks.get(guild_id)
    if existing is not None and not existing.done():
        return
    panel_update_events[guild_id] = asyncio.Event()
    panel_writer_tasks[guild_id] = asyncio.create_task(_panel_writer(guild_id))


def stop_panel_writer(guild_id: int) -> None:
    panel_update_events.pop(guild_id, None)
    panel_last_edit_at.pop(guild_id, None)
    task = panel_writer_tasks.pop(guild_id, None)
    if task is not None and not task.done():
        task.cancel()


async def refresh_panel(guild_id: int) -> None:
    """Signal the writer to redraw the existing panel. Never posts a new one —
    the panel is only ever created by the initial /play in queue_and_play."""
    if now_playing_messages.get(guild_id) is None:
        return
    ensure_panel_writer(guild_id)
    request_panel_update(guild_id)


async def _tick_now_playing(guild_id: int, song: dict) -> None:
    try:
        while True:
            await asyncio.sleep(NOW_PLAYING_REFRESH_SECONDS)
            if currently_playing.get(guild_id) is not song:
                return
            request_panel_update(guild_id)
    except asyncio.CancelledError:
        return
    finally:
        if now_playing_tasks.get(guild_id) is asyncio.current_task():
            now_playing_tasks.pop(guild_id, None)


def start_now_playing_ticker(guild_id: int, song: dict) -> None:
    existing = now_playing_tasks.get(guild_id)
    if existing is not None and not existing.done():
        existing.cancel()
    ensure_panel_writer(guild_id)
    now_playing_tasks[guild_id] = asyncio.create_task(_tick_now_playing(guild_id, song))
