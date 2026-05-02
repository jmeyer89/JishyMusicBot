import random

import discord
from discord import app_commands
from discord.ext import commands

import panel
import state


class QueueControlCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="queue", description="Show the current song queue.")
    async def queue_command(self, interaction: discord.Interaction) -> None:
        lines = panel.format_queue_lines(interaction.guild.id)
        if not lines:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="shuffle", description="Shuffle the current queue.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        queue = state.song_queues.get(guild_id, [])
        if len(queue) < 2:
            await interaction.response.send_message(
                "Not enough songs in the queue to shuffle.", ephemeral=True
            )
            return
        random.shuffle(queue)
        await panel.refresh_panel(guild_id)
        await panel.silent_ack(interaction)

    @app_commands.command(
        name="clearqueue", description="Clear the queue without stopping the current song."
    )
    async def clearqueue(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        pending_count = len(state.song_queues.get(guild_id, []))
        state.song_queues[guild_id] = []
        state.saved_queues.pop(guild_id, None)
        if pending_count == 0:
            await interaction.response.send_message("The queue is already empty.", ephemeral=True)
            return
        await panel.refresh_panel(guild_id)
        await panel.silent_ack(interaction)

    @app_commands.command(name="qremove", description="Remove a song from the queue by its position.")
    @app_commands.describe(position="Queue position to remove (see /queue)")
    async def qremove(self, interaction: discord.Interaction, position: int) -> None:
        guild_id = interaction.guild.id
        queue = state.song_queues.get(guild_id, [])
        if not queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        if position < 1 or position > len(queue):
            await interaction.response.send_message(
                f"Position must be between 1 and {len(queue)}.", ephemeral=True
            )
            return
        queue.pop(position - 1)
        await panel.refresh_panel(guild_id)
        await panel.silent_ack(interaction)

    @app_commands.command(name="playnext", description="Move a queued song to the top of the queue.")
    @app_commands.describe(position="Queue position to move to the top (see /queue)")
    async def playnext(self, interaction: discord.Interaction, position: int) -> None:
        guild_id = interaction.guild.id
        queue = state.song_queues.get(guild_id, [])
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
        await panel.refresh_panel(guild_id)
        await panel.silent_ack(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QueueControlCog(bot))
