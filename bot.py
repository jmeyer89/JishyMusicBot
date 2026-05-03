import discord
from discord.ext import commands

import bot_helpers
import config
import panel
import playlists


COG_MODULES = (
    "cogs.playback",
    "cogs.queue_control",
    "cogs.saved_queues",
    "cogs.admin",
)


class JishyBot(commands.Bot):
    async def setup_hook(self) -> None:
        for module in COG_MODULES:
            await self.load_extension(module)
        self.add_view(panel.QueueControlsView())


intents = discord.Intents.default()
bot = JishyBot(command_prefix="!", intents=intents)
bot.tree.interaction_check = bot_helpers.slash_interaction_check
config.load_config()
playlists.load_playlists()


@bot.event
async def on_ready() -> None:
    try:
        if config.INSTANT_SYNC_GUILD_IDS:
            for guild_id in config.INSTANT_SYNC_GUILD_IDS:
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
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(config.DISCORD_TOKEN)
