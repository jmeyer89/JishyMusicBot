"""Discord UI components for the now-playing panel.

Defines the persistent `QueueControlsView`, its child Selects/Modal, and the
`make_controls(guild_id)` factory that the writer calls each render. Every
component routes UI changes through `request_panel_update` rather than
editing the message directly.
"""
import time
import uuid

import discord

from config import (
    DEFAULT_VOLUME,
    PAUSED_TIMEOUT_SECONDS,
    audit,
    enforce_spam_guard,
    is_interaction_allowed,
)
from state import (
    currently_playing,
    queue_expanded,
    saved_queues,
    search_alternatives,
    song_queues,
    volume_levels,
)
from panel.render import format_time, parse_time
from panel.writer import request_panel_update
from panel.lifecycle import cancel_inactivity, schedule_inactivity
from panel.playlists_view import PlaylistsView


def _find_by_queue_id(guild_id: int, queue_id: str | None) -> int | None:
    if not queue_id:
        return None
    queue = song_queues.get(guild_id, [])
    for index, song in enumerate(queue):
        if song.get("queue_id") == queue_id:
            return index
    return None


def _queue_select_options(
    guild_id: int, *, exclude_first: bool, reverse: bool
) -> list[discord.SelectOption]:
    """Build SelectOption list for the queue Play/Remove dropdowns.

    `exclude_first` skips index 0 (the next-up song — already implicitly next,
    so showing it in 'Play next from queue' would be a no-op).
    `reverse` shows most-recently-queued first.
    """
    pending = song_queues.get(guild_id, [])
    start = 1 if exclude_first else 0
    indices = range(start, len(pending))
    if reverse:
        indices = reversed(indices)
    options: list[discord.SelectOption] = []
    for i in indices:
        song = pending[i]
        qid = song.get("queue_id")
        if not qid:
            continue
        title = (song.get("title") or "Unknown title")[:95]
        options.append(discord.SelectOption(label=f"{i + 1}. {title}", value=qid))
        if len(options) >= 25:
            break
    return options


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


class QueuePlaySelect(discord.ui.Select):
    def __init__(self, guild_id: int) -> None:
        super().__init__(
            placeholder="Play next from queue…",
            options=_queue_select_options(guild_id, exclude_first=True, reverse=True),
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
        super().__init__(
            placeholder="Remove from queue…",
            options=_queue_select_options(guild_id, exclude_first=False, reverse=False),
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
        new_entry = dict(song)
        new_entry["url"] = None
        new_entry["seek_to"] = float(seek_seconds)
        new_entry["queue_id"] = uuid.uuid4().hex
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
        await self._apply_volume_delta(interaction, -0.10)

    @discord.ui.button(label="100%", style=discord.ButtonStyle.secondary, disabled=True, row=1, custom_id="qc:vol_display")
    async def vol_display(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        return

    @discord.ui.button(label="Vol +10", style=discord.ButtonStyle.secondary, row=1, custom_id="qc:vol_up")
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._apply_volume_delta(interaction, +0.10)

    @discord.ui.button(label="Seek", style=discord.ButtonStyle.primary, row=1, custom_id="qc:seek")
    async def seek_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SeekModal())

    @discord.ui.button(label="Playlists", style=discord.ButtonStyle.secondary, row=1, custom_id="qc:playlists")
    async def playlists_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = PlaylistsView(interaction.guild.id, interaction.user.display_name)
        await interaction.response.send_message(
            content=view.content(), view=view, ephemeral=True
        )

    async def _apply_volume_delta(self, interaction: discord.Interaction, delta: float) -> None:
        guild_id = interaction.guild.id
        current = volume_levels.get(guild_id, DEFAULT_VOLUME)
        new_level = max(0.0, min(1.0, round(current + delta, 2)))
        volume_levels[guild_id] = new_level
        voice_client = interaction.guild.voice_client
        if voice_client is not None and voice_client.source is not None:
            voice_client.source.volume = new_level
        try:
            await interaction.response.defer()
        except discord.DiscordException:
            pass
        request_panel_update(guild_id)
