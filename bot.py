import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands

import spotify
from audio import extract_playlist_info, extract_song_info, is_playlist_url
from config import (
    AUDIT_LOG_FILE,
    DISCORD_TOKEN,
    INSTANT_SYNC_GUILD_IDS,
    MAX_QUEUE_LENGTH,
    PAUSED_TIMEOUT_SECONDS,
    SETUP_COMMANDS,
    audit,
    enforce_spam_guard,
    guild_config,
    is_interaction_allowed,
    load_config,
    save_config,
)
from panel import (
    build_panel_embed,
    cancel_inactivity,
    format_now_playing,
    format_queue_lines,
    make_controls,
    refresh_panel,
    schedule_inactivity,
    silent_ack,
    start_now_playing_ticker,
)
from player import play_next
from state import (
    announce_channels,
    currently_playing,
    now_playing_messages,
    now_playing_tasks,
    queue_expanded,
    saved_queues,
    song_queues,
    volume_levels,
)
import time


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


async def _delete_after(message: discord.Message, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except discord.DiscordException:
        pass
    except asyncio.CancelledError:
        return


async def _slash_interaction_check(interaction: discord.Interaction) -> bool:
    try:
        cmd_name = interaction.command.name if interaction.command else None
        if not await enforce_spam_guard(interaction, f"/{cmd_name or 'unknown'}"):
            return False
        if cmd_name in SETUP_COMMANDS and interaction.guild is not None:
            member = interaction.user
            if isinstance(member, discord.Member) and member.guild_permissions.administrator:
                audit(interaction, f"/{cmd_name}")
                return True
        allowed, reason = is_interaction_allowed(interaction)
        if not allowed:
            try:
                await interaction.response.send_message(
                    reason or "This bot is restricted here.", ephemeral=True
                )
            except (discord.InteractionResponded, discord.HTTPException):
                pass
            return False
        audit(interaction, f"/{cmd_name or 'unknown'}")
        return True
    except Exception as check_error:
        print(f"[security] slash check error: {check_error}")
        return False


@bot.event
async def on_ready() -> None:
    try:
        if INSTANT_SYNC_GUILD_IDS:
            for guild_id in INSTANT_SYNC_GUILD_IDS:
                guild = discord.Object(id=guild_id)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                print(f"Synced {len(synced)} slash command(s) to guild {guild_id}.")
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            print("Cleared global commands.")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global slash command(s).")
    except Exception as sync_error:
        print(f"Failed to sync command tree: {sync_error}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="play", description="Play or queue a song from Spotify, YouTube, or a search term.")
@app_commands.describe(query="A Spotify or YouTube URL, or a search term (Spotify is searched first)")
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
        if spotify.is_spotify_url(query):
            songs = await spotify.extract_spotify_tracks(query)
            if not songs:
                try:
                    await interaction.delete_original_response()
                except discord.DiscordException:
                    pass
                await interaction.followup.send("That Spotify link is empty or unavailable.", ephemeral=True)
                return
        elif query.startswith(("http://", "https://")):
            if is_playlist_url(query):
                songs = await extract_playlist_info(query)
                if not songs:
                    try:
                        await interaction.delete_original_response()
                    except discord.DiscordException:
                        pass
                    await interaction.followup.send("That playlist is empty or unavailable.", ephemeral=True)
                    return
            else:
                songs = [await extract_song_info(query)]
        else:
            spotify_hit: dict | None = None
            try:
                spotify_hit = await spotify.search_top_track(query)
            except spotify.SpotifyError as spotify_search_error:
                print(f"[spotify] search failed, falling back to YouTube: {spotify_search_error}")
            songs = [spotify_hit] if spotify_hit else [await extract_song_info(query)]
    except Exception as extraction_error:
        try:
            await interaction.delete_original_response()
        except discord.DiscordException:
            pass
        await interaction.followup.send(f"Could not fetch that: {extraction_error}", ephemeral=True)
        return
    remaining_slots = MAX_QUEUE_LENGTH - current_queue_length
    to_add = songs[:remaining_slots]
    for queued_song in to_add:
        queued_song["requester"] = interaction.user.display_name
    song_queues.setdefault(guild_id, []).extend(to_add)
    announce_channels[guild_id] = interaction.channel
    if not voice_client.is_playing() and not voice_client.is_paused():
        await play_next(guild_id, voice_client)
        started = currently_playing.get(guild_id, to_add[0])
        queue_expanded.pop(guild_id, None)
        sent = await interaction.followup.send(
            embed=build_panel_embed(guild_id),
            view=make_controls(guild_id),
        )
        now_playing_messages[guild_id] = sent
        start_now_playing_ticker(guild_id, started, sent)
    else:
        await refresh_panel(guild_id)
        if len(to_add) == 1:
            confirmation = f"Queued: **{to_add[0].get('title', 'song')}**"
        else:
            confirmation = f"Queued **{len(to_add)}** songs."
        skipped = len(songs) - len(to_add)
        if skipped > 0:
            confirmation += f" ({skipped} skipped — queue is full.)"
        try:
            await interaction.delete_original_response()
        except discord.DiscordException as delete_error:
            print(f"[/play] delete_original_response failed: {type(delete_error).__name__}: {delete_error}")
        try:
            sent_confirmation = await interaction.followup.send(confirmation)
        except discord.DiscordException as followup_error:
            print(f"[/play] followup.send failed: {type(followup_error).__name__}: {followup_error}")
            sent_confirmation = None
        if sent_confirmation is not None:
            asyncio.create_task(_delete_after(sent_confirmation, 10))


@bot.tree.command(name="skip", description="Skip the current song.")
async def skip(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    voice_client.stop()
    await silent_ack(interaction)


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
    schedule_inactivity(interaction.guild.id, interaction.client, PAUSED_TIMEOUT_SECONDS)
    await silent_ack(interaction)


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
    cancel_inactivity(interaction.guild.id)
    await silent_ack(interaction)


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
    cancel_inactivity(guild_id)
    voice_client = interaction.guild.voice_client
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    await silent_ack(interaction)


@bot.tree.command(name="queue", description="Show the current song queue.")
async def queue_command(interaction: discord.Interaction) -> None:
    lines = format_queue_lines(interaction.guild.id)
    if not lines:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="shuffle", description="Shuffle the current queue.")
async def shuffle(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    queue = song_queues.get(guild_id, [])
    if len(queue) < 2:
        await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        return
    random.shuffle(queue)
    await refresh_panel(guild_id)
    await silent_ack(interaction)


@bot.tree.command(name="clearqueue", description="Clear the queue without stopping the current song.")
async def clearqueue(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    pending_count = len(song_queues.get(guild_id, []))
    song_queues[guild_id] = []
    saved_queues.pop(guild_id, None)
    if pending_count == 0:
        await interaction.response.send_message("The queue is already empty.", ephemeral=True)
        return
    await refresh_panel(guild_id)
    await silent_ack(interaction)


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
    queue.pop(position - 1)
    await refresh_panel(guild_id)
    await silent_ack(interaction)


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
    await refresh_panel(guild_id)
    await silent_ack(interaction)


@bot.tree.command(name="nowplaying", description="Show the currently playing song.")
async def nowplaying(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    now_playing_song = currently_playing.get(guild_id)
    if now_playing_song is None:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{format_now_playing(now_playing_song)}\n{now_playing_song.get('webpage_url', '')}",
        ephemeral=True,
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
    await refresh_panel(guild_id)
    await silent_ack(interaction)


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
    cancel_inactivity(guild_id)
    await voice_client.disconnect()
    await silent_ack(interaction)


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
    lines.append(f"Audit log: `{AUDIT_LOG_FILE.name}` (on disk where the bot runs)")
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
    save_config()
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
    save_config()
    await interaction.response.send_message(msg, ephemeral=True)


bot.tree.interaction_check = _slash_interaction_check
load_config()


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
