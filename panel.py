import asyncio
import time
import uuid
import discord

from config import (
    DEFAULT_VOLUME,
    NOW_PLAYING_REFRESH_SECONDS,
    PANEL_MIN_EDIT_INTERVAL,
    INACTIVITY_TIMEOUT_SECONDS,
    PAUSED_TIMEOUT_SECONDS,
    audit,
    enforce_spam_guard,
    is_interaction_allowed,
)
from state import (
    announce_channels,
    song_queues,
    currently_playing,
    queue_expanded,
    volume_levels,
    now_playing_messages,
    now_playing_tasks,
    inactivity_tasks,
    saved_queues,
    search_alternatives,
    panel_writer_tasks,
    panel_update_events,
    panel_last_edit_at,
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


def make_controls(guild_id: int) -> "QueueControlsView":
    view = QueueControlsView()
    if queue_expanded.get(guild_id):
        view.show_queue.label = "Hide Queue"
    percent = int(round(volume_levels.get(guild_id, DEFAULT_VOLUME) * 100))
    view.vol_display.label = f"{percent}%"
    pending_count = len(song_queues.get(guild_id, []))
    if pending_count > 1:
        view.add_item(QueuePlaySelect(guild_id))
    if pending_count >= 1:
        view.add_item(QueueRemoveSelect(guild_id))
    alternatives = search_alternatives.get(guild_id)
    if alternatives:
        view.add_item(SearchPickerSelect(alternatives))
    return view


async def clear_panel(guild_id: int) -> None:
    """Pop the active panel message and remove it from chat. Falls back to stripping controls."""
    stop_panel_writer(guild_id)
    message = now_playing_messages.pop(guild_id, None)
    if message is None:
        return
    try:
        await message.delete()
        return
    except discord.DiscordException:
        pass
    try:
        await message.edit(view=None)
    except discord.DiscordException:
        pass


def request_panel_update(guild_id: int) -> None:
    """Signal the per-guild writer that the panel needs a redraw. Idempotent and cheap."""
    event = panel_update_events.get(guild_id)
    if event is not None:
        event.set()


async def _panel_writer(guild_id: int) -> None:
    """Single coalescing writer per guild: collapses bursts of update requests
    into the latest desired state and paces edits to stay under Discord's
    per-message PATCH bucket."""
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


async def silent_ack(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.delete_original_response()
        else:
            await interaction.response.defer(ephemeral=True)
            await interaction.delete_original_response()
    except discord.DiscordException:
        pass


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


def start_now_playing_ticker(guild_id: int, song: dict, message=None) -> None:
    existing = now_playing_tasks.get(guild_id)
    if existing is not None and not existing.done():
        existing.cancel()
    ensure_panel_writer(guild_id)
    now_playing_tasks[guild_id] = asyncio.create_task(_tick_now_playing(guild_id, song))


async def _auto_disconnect_after_inactivity(guild_id: int, seconds: int, bot: discord.Client) -> None:
    try:
        await asyncio.sleep(seconds)
        guild = bot.get_guild(guild_id)
        if guild is None:
            return
        voice_client = guild.voice_client
        if voice_client is None or voice_client.is_playing():
            return
        pending = list(song_queues.get(guild_id, []))
        interrupted = currently_playing.get(guild_id)
        if interrupted is not None:
            pending.insert(0, interrupted)
        for s in pending:
            s.pop("url", None)
            s.pop("started_at", None)
            s.pop("paused_total", None)
            s.pop("paused_at", None)
        if pending:
            saved_queues[guild_id] = pending
        song_queues.pop(guild_id, None)
        currently_playing.pop(guild_id, None)
        announce_channels.pop(guild_id, None)
        queue_expanded.pop(guild_id, None)
        search_alternatives.pop(guild_id, None)
        ticker = now_playing_tasks.pop(guild_id, None)
        if ticker is not None and not ticker.done():
            ticker.cancel()
        stop_panel_writer(guild_id)
        panel = now_playing_messages.pop(guild_id, None)
        if panel is not None:
            try:
                await panel.edit(view=None)
            except discord.DiscordException:
                pass
        try:
            await voice_client.disconnect(force=False)
        except discord.DiscordException:
            pass
    except asyncio.CancelledError:
        return
    finally:
        if inactivity_tasks.get(guild_id) is asyncio.current_task():
            inactivity_tasks.pop(guild_id, None)


def schedule_inactivity(guild_id: int, bot: discord.Client, seconds: int = INACTIVITY_TIMEOUT_SECONDS) -> None:
    existing = inactivity_tasks.get(guild_id)
    if existing is not None and not existing.done():
        existing.cancel()
    inactivity_tasks[guild_id] = asyncio.create_task(_auto_disconnect_after_inactivity(guild_id, seconds, bot))


def cancel_inactivity(guild_id: int) -> None:
    existing = inactivity_tasks.pop(guild_id, None)
    if existing is not None and not existing.done():
        existing.cancel()


def _find_by_queue_id(guild_id: int, queue_id: str | None) -> int | None:
    if not queue_id:
        return None
    queue = song_queues.get(guild_id, [])
    for index, song in enumerate(queue):
        if song.get("queue_id") == queue_id:
            return index
    return None


class QueuePlaySelect(discord.ui.Select):
    def __init__(self, guild_id: int) -> None:
        pending = song_queues.get(guild_id, [])
        options: list[discord.SelectOption] = []
        for i in range(len(pending) - 1, 0, -1):
            song = pending[i]
            qid = song.get("queue_id")
            if not qid:
                continue
            title = (song.get("title") or "Unknown title")[:95]
            options.append(discord.SelectOption(label=f"{i + 1}. {title}", value=qid))
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
        target_id = self.values[0] if self.values else None
        idx = _find_by_queue_id(guild_id, target_id)
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        if idx is None or idx == 0:
            return
        queue = song_queues[guild_id]
        song = queue.pop(idx)
        queue.insert(0, song)
        request_panel_update(guild_id)


class QueueRemoveSelect(discord.ui.Select):
    def __init__(self, guild_id: int) -> None:
        pending = song_queues.get(guild_id, [])
        options: list[discord.SelectOption] = []
        for i in range(len(pending)):
            song = pending[i]
            qid = song.get("queue_id")
            if not qid:
                continue
            title = (song.get("title") or "Unknown title")[:95]
            options.append(discord.SelectOption(label=f"{i + 1}. {title}", value=qid))
            if len(options) >= 25:
                break
        super().__init__(
            placeholder="Remove from queue…",
            options=options,
            min_values=1,
            max_values=1,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        target_id = self.values[0] if self.values else None
        idx = _find_by_queue_id(guild_id, target_id)
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        if idx is None:
            return
        song_queues[guild_id].pop(idx)
        request_panel_update(guild_id)


class SearchPickerSelect(discord.ui.Select):
    def __init__(self, candidates: list[dict]) -> None:
        options: list[discord.SelectOption] = []
        for index, song in enumerate(candidates[:25], start=1):
            title = (song.get("title") or "Unknown title")[:95]
            duration = song.get("duration")
            description = format_time(duration) if duration else "—"
            options.append(
                discord.SelectOption(
                    label=f"{index}. {title}",
                    description=description,
                    value=str(index - 1),
                )
            )
        super().__init__(
            placeholder="Pick another version to add…",
            options=options,
            min_values=1,
            max_values=1,
            row=4,
        )
        self.candidates = candidates

    async def callback(self, interaction: discord.Interaction) -> None:
        # Lazy import: bot_helpers imports panel, so a top-level import would cycle.
        from bot_helpers import ensure_voice, queue_and_play

        await interaction.response.defer()
        chosen = dict(self.candidates[int(self.values[0])])
        voice_client = await ensure_voice(interaction)
        if voice_client is None:
            return
        await queue_and_play(
            interaction=interaction,
            voice_client=voice_client,
            songs=[chosen],
        )


class SeekModal(discord.ui.Modal, title="Seek"):
    position = discord.ui.TextInput(
        label="Time (mm:ss or seconds)",
        placeholder="1:30",
        required=True,
        max_length=8,
    )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await enforce_spam_guard(interaction, "modal:seek")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        seconds = parse_time(self.position.value)
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
            "queue_id": uuid.uuid4().hex,
        }
        song_queues.setdefault(guild_id, []).insert(0, new_entry)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await interaction.response.defer()


class QueueControlsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        custom_id = (interaction.data or {}).get("custom_id") or "unknown"
        if not await enforce_spam_guard(interaction, f"component:{custom_id}"):
            return False
        allowed, reason = is_interaction_allowed(interaction)
        if not allowed:
            try:
                await interaction.response.send_message(
                    reason or "This bot is restricted here.", ephemeral=True
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return False
        audit(interaction, f"component:{custom_id}")
        return True

    @discord.ui.button(label="Pause/Play", style=discord.ButtonStyle.success, custom_id="qc:toggle")
    async def pause_play_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        if voice_client.is_playing():
            voice_client.pause()
            song = currently_playing.get(guild_id)
            if song is not None and song.get("paused_at") is None:
                song["paused_at"] = time.monotonic()
            schedule_inactivity(guild_id, interaction.client, PAUSED_TIMEOUT_SECONDS)
            await interaction.response.defer()
            request_panel_update(guild_id)
            return
        if voice_client.is_paused():
            voice_client.resume()
            song = currently_playing.get(guild_id)
            if song is not None and song.get("paused_at") is not None:
                song["paused_total"] = song.get("paused_total", 0.0) + (time.monotonic() - song["paused_at"])
                song["paused_at"] = None
            cancel_inactivity(guild_id)
            await interaction.response.defer()
            request_panel_update(guild_id)
            return
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger, custom_id="qc:skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        voice_client.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Show Queue", style=discord.ButtonStyle.secondary, custom_id="qc:show_queue")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        queue_expanded[guild_id] = not queue_expanded.get(guild_id, False)
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        request_panel_update(guild_id)

    @discord.ui.button(label="Clear Queue", style=discord.ButtonStyle.danger, custom_id="qc:clear")
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        pending_count = len(song_queues.get(guild_id, []))
        if pending_count == 0:
            await interaction.response.send_message("The queue is already empty.", ephemeral=True)
            return
        song_queues[guild_id] = []
        saved_queues.pop(guild_id, None)
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        request_panel_update(guild_id)

    @discord.ui.button(label="Vol −10", style=discord.ButtonStyle.secondary, row=1, custom_id="qc:vol_down")
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        current = volume_levels.get(guild_id, DEFAULT_VOLUME)
        new_level = max(0.0, round(current - 0.10, 2))
        volume_levels[guild_id] = new_level
        voice_client = interaction.guild.voice_client
        if voice_client is not None and voice_client.source is not None:
            voice_client.source.volume = new_level
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        request_panel_update(guild_id)

    @discord.ui.button(label="100%", style=discord.ButtonStyle.secondary, disabled=True, row=1, custom_id="qc:vol_display")
    async def vol_display(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        return

    @discord.ui.button(label="Vol +10", style=discord.ButtonStyle.secondary, row=1, custom_id="qc:vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild_id = interaction.guild.id
        current = volume_levels.get(guild_id, DEFAULT_VOLUME)
        new_level = min(1.0, round(current + 0.10, 2))
        volume_levels[guild_id] = new_level
        voice_client = interaction.guild.voice_client
        if voice_client is not None and voice_client.source is not None:
            voice_client.source.volume = new_level
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        request_panel_update(guild_id)

    @discord.ui.button(label="Seek", style=discord.ButtonStyle.primary, row=1, custom_id="qc:seek")
    async def seek_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SeekModal())
