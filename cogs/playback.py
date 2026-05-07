import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

import audio
import bot_helpers
import config
import panel
import spotify
import state


class PlaybackCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="play",
        description="Play or queue a song from Spotify, YouTube, or a search term.",
    )
    @app_commands.describe(
        query="A Spotify or YouTube URL, or a search term (Spotify is searched first)",
    )
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        voice_client = await bot_helpers.ensure_voice(interaction)
        if voice_client is None:
            await interaction.edit_original_response(
                content="You need to be in a voice channel first."
            )
            return
        guild_id = interaction.guild.id
        saved = state.saved_queues.pop(guild_id, None)
        if saved:
            state.song_queues.setdefault(guild_id, []).extend(saved)
        if len(state.song_queues.get(guild_id, [])) >= config.MAX_QUEUE_LENGTH:
            await interaction.edit_original_response(
                content=(
                    f"The queue is full ({config.MAX_QUEUE_LENGTH} songs max). "
                    "Wait for some to finish or use /stop to clear it."
                )
            )
            return
        picker_alternatives: list[dict] | None = None
        try:
            if spotify.is_spotify_url(query):
                songs = await spotify.extract_spotify_tracks(query)
                if not songs:
                    await interaction.edit_original_response(
                        content="That Spotify link is empty or unavailable."
                    )
                    return
            elif query.startswith(("http://", "https://")):
                if audio.is_playlist_url(query):
                    songs = await audio.extract_playlist_info(query)
                    if not songs:
                        await interaction.edit_original_response(
                            content="That playlist is empty or unavailable."
                        )
                        return
                else:
                    songs = [await audio.extract_song_info(query)]
            else:
                candidates: list[dict] = []
                try:
                    candidates = await spotify.search_tracks(query, limit=10)
                except spotify.SpotifyError as spotify_search_error:
                    print(f"[spotify] search failed, falling back to YouTube: {spotify_search_error}")
                if candidates:
                    songs = [candidates[0]]
                    picker_alternatives = candidates[1:] or None
                else:
                    songs = [await audio.extract_song_info(query)]
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content="That took too long to load. Try a different link or a smaller playlist."
            )
            return
        except Exception as extraction_error:
            await interaction.edit_original_response(
                content=f"Could not fetch that: {extraction_error}"
            )
            return
        if picker_alternatives:
            state.search_alternatives[guild_id] = picker_alternatives
        # Fill the deferred slot first so the panel posts as a real followup
        # rather than getting collapsed into the original response.
        try:
            await interaction.edit_original_response(content=f"Queued: **{songs[0].get('title', 'song')}**")
        except discord.DiscordException as ack_edit_error:
            print(f"[/play] ack edit failed: {type(ack_edit_error).__name__}: {ack_edit_error}")
        await bot_helpers.queue_and_play(
            interaction=interaction,
            voice_client=voice_client,
            songs=songs,
        )
        try:
            await interaction.delete_original_response()
        except discord.DiscordException as delete_error:
            print(f"[/play] delete failed: {type(delete_error).__name__}: {delete_error}")

    @app_commands.command(name="skip", description="Skip the current song.")
    async def skip(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        voice_client.stop()
        await panel.silent_ack(interaction)

    @app_commands.command(name="pause", description="Pause playback.")
    async def pause(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing to pause.", ephemeral=True)
            return
        voice_client.pause()
        song = state.currently_playing.get(interaction.guild.id)
        if song is not None and song.get("paused_at") is None:
            song["paused_at"] = time.monotonic()
        panel.schedule_inactivity(
            interaction.guild.id, interaction.client, config.PAUSED_TIMEOUT_SECONDS
        )
        await panel.silent_ack(interaction)

    @app_commands.command(name="resume", description="Resume paused playback.")
    async def resume(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_paused():
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)
            return
        voice_client.resume()
        song = state.currently_playing.get(interaction.guild.id)
        if song is not None and song.get("paused_at") is not None:
            song["paused_total"] = song.get("paused_total", 0.0) + (time.monotonic() - song["paused_at"])
            song["paused_at"] = None
        panel.cancel_inactivity(interaction.guild.id)
        await panel.silent_ack(interaction)

    @app_commands.command(name="stop", description="Stop playback and clear the queue.")
    async def stop(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        state.song_queues[guild_id] = []
        state.currently_playing.pop(guild_id, None)
        state.saved_queues.pop(guild_id, None)
        state.queue_expanded.pop(guild_id, None)
        state.search_alternatives.pop(guild_id, None)
        existing_task = state.now_playing_tasks.pop(guild_id, None)
        if existing_task is not None and not existing_task.done():
            existing_task.cancel()
        panel.cancel_inactivity(guild_id)
        voice_client = interaction.guild.voice_client
        if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
        await panel.clear_panel(guild_id)
        await panel.silent_ack(interaction)

    @app_commands.command(name="nowplaying", description="Show the currently playing song.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        now_playing_song = state.currently_playing.get(guild_id)
        if now_playing_song is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{panel.format_now_playing(now_playing_song)}\n{now_playing_song.get('webpage_url', '')}",
            ephemeral=True,
        )

    @app_commands.command(name="volume", description="Set playback volume (0-100).")
    @app_commands.describe(level="Volume level from 0 to 100")
    async def volume(self, interaction: discord.Interaction, level: int) -> None:
        if level < 0 or level > 100:
            await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        state.volume_levels[guild_id] = level / 100
        voice_client = interaction.guild.voice_client
        if voice_client is not None and voice_client.source is not None:
            voice_client.source.volume = level / 100
        await panel.refresh_panel(guild_id)
        await panel.silent_ack(interaction)

    @app_commands.command(name="leave", description="Disconnect the bot from voice.")
    async def leave(self, interaction: discord.Interaction) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
            return
        guild_id = interaction.guild.id
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
        state.song_queues.pop(guild_id, None)
        state.currently_playing.pop(guild_id, None)
        state.announce_channels.pop(guild_id, None)
        state.queue_expanded.pop(guild_id, None)
        state.search_alternatives.pop(guild_id, None)
        existing_task = state.now_playing_tasks.pop(guild_id, None)
        if existing_task is not None and not existing_task.done():
            existing_task.cancel()
        panel.cancel_inactivity(guild_id)
        await panel.clear_panel(guild_id)
        await voice_client.disconnect()
        await panel.silent_ack(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlaybackCog(bot))
