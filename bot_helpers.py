import asyncio

import discord

import config
import panel
import player
import state


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
    state.song_queues.setdefault(guild_id, []).extend(to_add)
    state.announce_channels[guild_id] = interaction.channel
    if not voice_client.is_playing() and not voice_client.is_paused():
        started_fresh = await player.play_next(guild_id, voice_client)
        if started_fresh:
            started = state.currently_playing.get(guild_id, to_add[0])
            state.queue_expanded.pop(guild_id, None)
            try:
                sent = await interaction.followup.send(
                    embed=panel.build_panel_embed(guild_id),
                    view=panel.make_controls(guild_id),
                )
            except discord.DiscordException as panel_send_error:
                print(f"[queue_and_play] panel send failed: {type(panel_send_error).__name__}: {panel_send_error}")
                return
            state.now_playing_messages[guild_id] = sent
            panel.start_now_playing_ticker(guild_id, started, sent)
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
