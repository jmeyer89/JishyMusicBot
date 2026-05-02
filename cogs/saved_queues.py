import discord
from discord import app_commands
from discord.ext import commands

import bot_helpers
import playlists
import state


class SavedQueuesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    q_group = app_commands.Group(
        name="q",
        description="Save and load named queues for this server.",
    )

    @q_group.command(name="save", description="Save the current queue under a name.")
    @app_commands.describe(name="Name to save under (max 32 chars).")
    async def save(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild is None:
            return
        guild_id = interaction.guild.id
        pending = list(state.song_queues.get(guild_id, []))
        interrupted = state.currently_playing.get(guild_id)
        if interrupted is not None:
            pending.insert(0, interrupted)
        _, message = playlists.save_queue(
            guild_id, name, pending, interaction.user.display_name
        )
        await interaction.response.send_message(message, ephemeral=True)

    @q_group.command(name="load", description="Append a saved queue to the current queue.")
    @app_commands.describe(name="Name of the saved queue to load.")
    async def load(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild is None:
            return
        songs = playlists.load_queue(interaction.guild.id, name)
        if songs is None:
            await interaction.response.send_message(
                f"No saved queue named **{name}** found. Use `/q list` to see saved queues.",
                ephemeral=True,
            )
            return
        if not songs:
            await interaction.response.send_message(
                f"Saved queue **{name}** is empty.", ephemeral=True
            )
            return
        await interaction.response.defer()
        voice_client = await bot_helpers.ensure_voice(interaction)
        if voice_client is None:
            await interaction.followup.send(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return
        await bot_helpers.queue_and_play(
            interaction=interaction,
            voice_client=voice_client,
            songs=songs,
        )
        try:
            await interaction.delete_original_response()
        except discord.DiscordException as delete_error:
            print(f"[/q load] delete_original_response failed: {type(delete_error).__name__}: {delete_error}")

    @q_group.command(name="list", description="List saved queues for this server.")
    async def list_queues(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        saved = playlists.list_for_guild(interaction.guild.id)
        if not saved:
            await interaction.response.send_message(
                "No saved queues in this server. Use `/q save <name>` to create one.",
                ephemeral=True,
            )
            return
        lines = [f"**Saved queues ({len(saved)}):**"]
        for normalized, entry in sorted(saved.items()):
            display = entry.get("display_name", normalized)
            n = len(entry.get("songs", []))
            by = entry.get("saved_by", "unknown")
            lines.append(f"• **{display}** — {n} song(s) · saved by {by}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @q_group.command(name="delete", description="Delete a saved queue.")
    @app_commands.describe(name="Name of the saved queue to delete.")
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        if interaction.guild is None:
            return
        if playlists.delete_queue(interaction.guild.id, name):
            await interaction.response.send_message(
                f"Deleted saved queue **{name}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"No saved queue named **{name}** found.", ephemeral=True
            )

    @load.autocomplete("name")
    @delete.autocomplete("name")
    async def _name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        current_lower = current.strip().lower()
        names = playlists.names_for_guild(interaction.guild.id)
        matches = [n for n in names if current_lower in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SavedQueuesCog(bot))
