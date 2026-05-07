"""Ephemeral, per-click playlist-management UI.

Opened from the `qc:playlists` button on the main panel. Each click creates
its own short-lived `PlaylistsView` (5-minute timeout) so multiple users
can have independent playlist conversations with the bot at the same time
without stepping on each other.

The view re-renders itself in place after each action via
`interaction.response.edit_message(...)` so the user stays in one chat slot.
"""
import discord

import playlists
import state
from panel.render import format_time


PLAYLIST_VIEW_TIMEOUT_SECONDS = 300

_MODE_MAIN = "main"
_MODE_QUEUE_PICKER = "queue_picker"


class PlaylistsView(discord.ui.View):
    """Per-click ephemeral view holding playlist-management UI state."""

    def __init__(self, guild_id: int, user_name: str) -> None:
        super().__init__(timeout=PLAYLIST_VIEW_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.user_name = user_name
        self.selected: str | None = None  # display name of selected playlist
        self.mode: str = _MODE_MAIN
        self._render()

    # ---- Rendering -------------------------------------------------------

    def _render(self) -> None:
        self.clear_items()
        names = playlists.names_for_guild(self.guild_id)
        if names:
            self.add_item(_PlaylistSelect(parent=self, names=names))
        for button in self._build_action_buttons():
            self.add_item(button)
        if self.mode == _MODE_QUEUE_PICKER:
            queue = state.song_queues.get(self.guild_id, [])
            if queue:
                self.add_item(_QueueSongPickerSelect(parent=self, queue=queue))

    def _build_action_buttons(self) -> list[discord.ui.Button]:
        has_selection = self.selected is not None
        has_now_playing = state.currently_playing.get(self.guild_id) is not None
        has_queue = bool(state.song_queues.get(self.guild_id))
        selected_data = self._selected_data()
        is_empty = selected_data is not None and not selected_data.get("songs")

        buttons: list[discord.ui.Button] = []

        create_btn = discord.ui.Button(
            label="Create new…", style=discord.ButtonStyle.success, row=1
        )
        create_btn.callback = self._on_create_clicked
        buttons.append(create_btn)

        load_btn = discord.ui.Button(
            label="Load",
            style=discord.ButtonStyle.primary,
            row=1,
            disabled=not has_selection or is_empty,
        )
        load_btn.callback = self._on_load_clicked
        buttons.append(load_btn)

        add_current_btn = discord.ui.Button(
            label="Add current song",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=not has_selection or not has_now_playing,
        )
        add_current_btn.callback = self._on_add_current_clicked
        buttons.append(add_current_btn)

        add_queue_btn = discord.ui.Button(
            label="Add from queue…",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=not has_selection or not has_queue,
        )
        add_queue_btn.callback = self._on_add_queue_clicked
        buttons.append(add_queue_btn)

        delete_btn = discord.ui.Button(
            label="Delete",
            style=discord.ButtonStyle.danger,
            row=1,
            disabled=not has_selection,
        )
        delete_btn.callback = self._on_delete_clicked
        buttons.append(delete_btn)

        return buttons

    def _selected_data(self) -> dict | None:
        if not self.selected:
            return None
        for entry in playlists.list_for_guild(self.guild_id).values():
            if entry.get("display_name") == self.selected:
                return entry
        return None

    def content(self) -> str:
        """Default header text for the ephemeral message."""
        names = playlists.names_for_guild(self.guild_id)
        if self.mode == _MODE_QUEUE_PICKER and self.selected:
            return f"Selected: **{self.selected}** — pick a queued song to add."
        if not names and not self.selected:
            return "You don't have any playlists yet. Click **Create new…** to start."
        if self.selected:
            data = self._selected_data()
            n = len(data.get("songs", [])) if data else 0
            return f"Selected: **{self.selected}** — {n} song(s)."
        return f"You have **{len(names)}** playlist(s). Pick one to manage, or create a new one."

    async def _update(self, interaction: discord.Interaction, status: str | None = None) -> None:
        self._render()
        text = status if status is not None else self.content()
        try:
            await interaction.response.edit_message(content=text, view=self)
        except discord.InteractionResponded:
            try:
                await interaction.edit_original_response(content=text, view=self)
            except discord.DiscordException as edit_err:
                print(f"[PlaylistsView] edit fallback failed: {type(edit_err).__name__}: {edit_err}")

    # ---- Action handlers -------------------------------------------------

    async def _on_create_clicked(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_CreatePlaylistModal(parent=self))

    async def _on_load_clicked(self, interaction: discord.Interaction) -> None:
        # Lazy import: bot_helpers imports panel at module load.
        from bot_helpers import ensure_voice, queue_and_play

        if not self.selected:
            return
        songs = playlists.load_queue(self.guild_id, self.selected)
        if not songs:
            await self._update(interaction, status=f"**{self.selected}** is empty.")
            return
        await interaction.response.defer(ephemeral=True)
        voice_client = await ensure_voice(interaction)
        if voice_client is None:
            await interaction.edit_original_response(
                content="You need to be in a voice channel to load a playlist.",
                view=self,
            )
            return
        await queue_and_play(
            interaction=interaction, voice_client=voice_client, songs=songs
        )
        self._render()
        await interaction.edit_original_response(
            content=f"Loaded **{self.selected}** ({len(songs)} song(s)).",
            view=self,
        )

    async def _on_add_current_clicked(self, interaction: discord.Interaction) -> None:
        if not self.selected:
            return
        song = state.currently_playing.get(self.guild_id)
        if not song:
            await self._update(interaction, status="Nothing is playing right now.")
            return
        _, message = playlists.add_song_to_playlist(
            self.guild_id, self.selected, song, self.user_name
        )
        await self._update(interaction, status=message)

    async def _on_add_queue_clicked(self, interaction: discord.Interaction) -> None:
        if not state.song_queues.get(self.guild_id):
            await self._update(interaction, status="The queue is empty.")
            return
        self.mode = _MODE_QUEUE_PICKER
        await self._update(interaction)

    async def _on_delete_clicked(self, interaction: discord.Interaction) -> None:
        if not self.selected:
            return
        deleted = playlists.delete_queue(self.guild_id, self.selected)
        message = (
            f"Deleted **{self.selected}**." if deleted
            else f"Could not delete **{self.selected}**."
        )
        self.selected = None
        self.mode = _MODE_MAIN
        await self._update(interaction, status=message)

    # ---- Sub-component callbacks ----------------------------------------

    async def select_playlist(self, interaction: discord.Interaction, name: str) -> None:
        self.selected = name
        self.mode = _MODE_MAIN
        await self._update(interaction)

    async def add_queued_song(self, interaction: discord.Interaction, queue_id: str) -> None:
        if not self.selected:
            await self._update(interaction)
            return
        queue = state.song_queues.get(self.guild_id, [])
        target = next((s for s in queue if s.get("queue_id") == queue_id), None)
        if target is None:
            self.mode = _MODE_MAIN
            await self._update(interaction, status="That song is no longer in the queue.")
            return
        _, message = playlists.add_song_to_playlist(
            self.guild_id, self.selected, target, self.user_name
        )
        self.mode = _MODE_MAIN
        await self._update(interaction, status=message)


class _PlaylistSelect(discord.ui.Select):
    def __init__(self, parent: PlaylistsView, names: list[str]) -> None:
        options = [
            discord.SelectOption(
                label=name[:95],
                value=name[:100],
                default=(name == parent.selected),
            )
            for name in names[:25]
        ]
        super().__init__(
            placeholder="Pick a playlist…",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent.select_playlist(interaction, self.values[0])


class _QueueSongPickerSelect(discord.ui.Select):
    def __init__(self, parent: PlaylistsView, queue: list[dict]) -> None:
        options: list[discord.SelectOption] = []
        for index, song in enumerate(queue[:25], start=1):
            qid = song.get("queue_id")
            if not qid:
                continue
            title = (song.get("title") or "Unknown title")[:95]
            duration = song.get("duration")
            description = format_time(duration) if duration else "—"
            options.append(
                discord.SelectOption(
                    label=f"{index}. {title}",
                    description=description,
                    value=qid,
                )
            )
        super().__init__(
            placeholder="Pick a queued song to add…",
            options=options,
            min_values=1,
            max_values=1,
            row=2,
        )
        self.parent = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent.add_queued_song(interaction, self.values[0])


class _CreatePlaylistModal(discord.ui.Modal, title="New playlist"):
    name = discord.ui.TextInput(
        label="Playlist name",
        placeholder="My favorites",
        min_length=1,
        max_length=32,
        required=True,
    )

    def __init__(self, parent: PlaylistsView) -> None:
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        success, message = playlists.create_playlist(
            self.parent.guild_id, self.name.value, self.parent.user_name
        )
        if success:
            self.parent.selected = self.name.value.strip()[:32]
        self.parent.mode = _MODE_MAIN
        await self.parent._update(interaction, status=message)
