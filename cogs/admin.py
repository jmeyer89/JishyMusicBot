import discord
from discord import app_commands
from discord.ext import commands

import config


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="music_show_config", description="Show current bot security settings."
    )
    @app_commands.default_permissions(administrator=True)
    async def music_show_config(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        cfg = config.guild_config.get(interaction.guild.id, {})
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
        lines.append(f"Audit log: `{config.AUDIT_LOG_FILE.name}` (on disk where the bot runs)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="music_set_channel", description="Restrict the bot to one channel (admin only)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to restrict to. Leave blank to clear restriction.")
    async def music_set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        cfg = config.guild_config.setdefault(interaction.guild.id, {})
        if channel is None:
            cfg.pop("allowed_channels", None)
            msg = "Channel restriction cleared. The bot now works in all channels."
        else:
            cfg["allowed_channels"] = [channel.id]
            msg = f"The bot is now restricted to {channel.mention}."
        config.save_config()
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="music_set_role", description="Require a role to use the bot (admin only)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="Role required. Leave blank to clear requirement.")
    async def music_set_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        cfg = config.guild_config.setdefault(interaction.guild.id, {})
        if role is None:
            cfg.pop("allowed_role", None)
            msg = "Role requirement cleared. All members can use the bot."
        else:
            cfg["allowed_role"] = role.id
            msg = f"The bot now requires the {role.mention} role."
        config.save_config()
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
