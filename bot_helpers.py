import asyncio
import uuid

import discord

import audio
import config
import panel
import player
import spotify
import state


class QueryResolutionError(Exception):
    """Raised by resolve_query for known, user-facing failure modes (empty
    Spotify link, empty playlist, etc.). The message is shown verbatim to the
    user."""


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    user_voice_state = interaction.user.voice
    if user_voice_state is None or user_voice_state.channel is None:
        return None
    target_channel = user_voice_state.channel
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        return await target_channel.connect()
    if voice_client.channel != target_channel:
        await voice_client.move_to(target_channel)
    return voice_client


async def delete_after(message: discord.Message, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except discord.DiscordException:
        pass
    except asyncio.CancelledError:
        return


async def send_transient(
    interaction: discord.Interaction, content: str, delay: float = 5
) -> None:
    """Send a public followup message and auto-delete it after `delay` seconds."""
    try:
        sent = await interaction.followup.send(content)
    except discord.DiscordException as send_error:
        print(f"[send_transient] send failed: {type(send_error).__name__}: {send_error}")
        return
    if sent is not None:
        asyncio.create_task(delete_after(sent, delay))


async def resolve_query(query: str) -> tuple[list[dict], list[dict] | None]:
    """Resolve a /play argument into songs to queue.

    Returns (songs, picker_alternatives_or_None). Routes the query through
    Spotify (URLs first, free-text search second) and falls back to YouTube.

    Raises QueryResolutionError for known failure modes (empty link, etc.).
    Raises asyncio.TimeoutError or other exceptions from the underlying
    extractors for unexpected failures — callers should catch both.
    """
    if spotify.is_spotify_url(query):
        songs = await spotify.extract_spotify_tracks(query)
        if not songs:
            raise QueryResolutionError("That Spotify link is empty or unavailable.")
        return songs, None
    if query.startswith(("http://", "https://")):
        if audio.is_playlist_url(query):
            songs = await audio.extract_playlist_info(query)
            if not songs:
                raise QueryResolutionError("That playlist is empty or unavailable.")
            return songs, None
        return [await audio.extract_song_info(query)], None
    # Free-text: try Spotify first (better titles), fall back to YouTube search.
    candidates: list[dict] = []
    try:
        candidates = await spotify.search_tracks(query, limit=10)
    except spotify.SpotifyError as spotify_search_error:
        print(f"[spotify] search failed, falling back to YouTube: {spotify_search_error}")
    if candidates:
        return [candidates[0]], (candidates[1:] or None)
    return [await audio.extract_song_info(query)], None


async def teardown_guild_session(
    guild_id: int,
    voice_client: discord.VoiceClient | None,
    *,
    disconnect: bool,
    preserve_queue: bool,
    delete_panel: bool,
) -> None:
    """Tear down a guild's playback session.

    Used by /stop (no disconnect, no preserve, delete panel),
    /leave (disconnect, preserve, delete panel), and the inactivity
    auto-disconnect (disconnect, preserve, strip panel controls but keep
    the message in chat for context).
    """
    if preserve_queue:
        pending = list(state.song_queues.get(guild_id, []))
        interrupted = state.currently_playing.get(guild_id)
        if interrupted is not None:
            pending.insert(0, interrupted)
        for queued_song in pending:
            queued_song.pop("url", None)
            queued_song.pop("started_at", None)
            queued_song.pop("paused_total", None)
            queued_song.pop("paused_at", None)
        if pending:
            state.saved_queues[guild_id] = pending
    else:
        state.saved_queues.pop(guild_id, None)
    state.song_queues.pop(guild_id, None)
    state.currently_playing.pop(guild_id, None)
    state.announce_channels.pop(guild_id, None)
    state.queue_expanded.pop(guild_id, None)
    state.search_alternatives.pop(guild_id, None)
    existing_task = state.now_playing_tasks.pop(guild_id, None)
    if existing_task is not None and not existing_task.done():
        existing_task.cancel()
    panel.cancel_inactivity(guild_id)
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    if delete_panel:
        await panel.clear_panel(guild_id)
    else:
        panel.stop_panel_writer(guild_id)
        message = state.now_playing_messages.pop(guild_id, None)
        if message is not None:
            try:
                await message.edit(view=None)
            except discord.DiscordException:
                pass
    if disconnect and voice_client is not None:
        try:
            await voice_client.disconnect(force=False)
        except discord.DiscordException:
            pass


async def queue_and_play(
    interaction: discord.Interaction,
    voice_client: discord.VoiceClient,
    songs: list[dict],
) -> None:
    guild_id = interaction.guild.id
    if not voice_client.is_connected():
        await send_transient(interaction, "I'm no longer connected to voice.")
        return
    current_queue_length = len(state.song_queues.get(guild_id, []))
    remaining_slots = config.MAX_QUEUE_LENGTH - current_queue_length
    if remaining_slots <= 0:
        await send_transient(interaction, f"The queue is full ({config.MAX_QUEUE_LENGTH} songs max).")
        return
    to_add = songs[:remaining_slots]
    for queued_song in to_add:
        queued_song["requester"] = interaction.user.display_name
        queued_song.setdefault("queue_id", uuid.uuid4().hex)
    state.song_queues.setdefault(guild_id, []).extend(to_add)
    state.announce_channels[guild_id] = interaction.channel
    if not voice_client.is_playing() and not voice_client.is_paused():
        started_fresh = await player.play_next(guild_id, voice_client)
        if started_fresh:
            started = state.currently_playing.get(guild_id, to_add[0])
            state.queue_expanded.pop(guild_id, None)
            existing_panel = state.now_playing_messages.get(guild_id)
            if existing_panel is None:
                # Send via the channel (not interaction.followup) so the resulting
                # Message edits with the bot token. Followup messages are webhook
                # messages whose tokens expire after 15 minutes (error 50027).
                try:
                    sent = await interaction.channel.send(
                        embed=panel.build_panel_embed(guild_id),
                        view=panel.make_controls(guild_id),
                    )
                except discord.DiscordException as panel_send_error:
                    print(f"[queue_and_play] panel send failed: {type(panel_send_error).__name__}: {panel_send_error}")
                    return
                state.now_playing_messages[guild_id] = sent
            panel.ensure_panel_writer(guild_id)
            panel.request_panel_update(guild_id)
            panel.start_now_playing_ticker(guild_id, started)
            return
    await panel.refresh_panel(guild_id)
    if len(to_add) == 1:
        confirmation = f"Queued: **{to_add[0].get('title', 'song')}**"
    else:
        confirmation = f"Queued **{len(to_add)}** songs."
    skipped = len(songs) - len(to_add)
    if skipped > 0:
        confirmation += f" ({skipped} skipped — queue is full.)"
    await send_transient(interaction, confirmation)


async def slash_interaction_check(interaction: discord.Interaction) -> bool:
    try:
        cmd_name = interaction.command.name if interaction.command else None
        if not await config.enforce_spam_guard(interaction, f"/{cmd_name or 'unknown'}"):
            return False
        if cmd_name in config.SETUP_COMMANDS and interaction.guild is not None:
            member = interaction.user
            if isinstance(member, discord.Member) and member.guild_permissions.administrator:
                config.audit(interaction, f"/{cmd_name}")
                return True
        allowed, reason = config.is_interaction_allowed(interaction)
        if not allowed:
            try:
                await interaction.response.send_message(
                    reason or "This bot is restricted here.", ephemeral=True
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return False
        config.audit(interaction, f"/{cmd_name or 'unknown'}")
        return True
    except Exception as check_error:
        print(f"[security] slash check error: {check_error}")
        return False
