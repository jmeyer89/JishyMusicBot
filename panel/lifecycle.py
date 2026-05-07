"""Panel cleanup, interaction acks, and idle-timeout disconnect.

Owns the inactivity-timer task and the panel-removal helpers. The
auto-disconnect path delegates the actual session teardown to
`bot_helpers.teardown_guild_session` (lazy-imported to avoid a cycle).
"""
import asyncio

import discord

from config import INACTIVITY_TIMEOUT_SECONDS
from state import (
    inactivity_tasks,
    now_playing_messages,
)
from panel.writer import stop_panel_writer


async def silent_ack(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.delete_original_response()
        else:
            await interaction.response.defer(ephemeral=True)
            await interaction.delete_original_response()
    except discord.DiscordException:
        pass


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


async def _auto_disconnect_after_inactivity(guild_id: int, seconds: int, bot: discord.Client) -> None:
    try:
        await asyncio.sleep(seconds)
        guild = bot.get_guild(guild_id)
        if guild is None:
            return
        voice_client = guild.voice_client
        if voice_client is None or voice_client.is_playing():
            return
        # Lazy import: bot_helpers imports panel at module load.
        from bot_helpers import teardown_guild_session
        await teardown_guild_session(
            guild_id,
            voice_client,
            disconnect=True,
            preserve_queue=True,
            delete_panel=False,
        )
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
    existing = inactivity_tasks.get(guild_id)
    # If we're being called from inside the inactivity task itself (via
    # teardown_guild_session), don't cancel ourselves mid-cleanup — the task's
    # finally clause will pop the entry on exit.
    if existing is asyncio.current_task():
        return
    inactivity_tasks.pop(guild_id, None)
    if existing is not None and not existing.done():
        existing.cancel()
