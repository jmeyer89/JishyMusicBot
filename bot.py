import os
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
INSTANT_SYNC_GUILD_IDS = [205409317283823627, 708832660444938244]

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

song_queues: dict[int, list[dict]] = {}
currently_playing: dict[int, dict] = {}
announce_channels: dict[int, discord.abc.Messageable] = {}

MAX_QUEUE_LENGTH = 30

YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "skip_download": True,
    "extractor_args": {"youtube": {"player_client": ["android", "tv", "web"]}},
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)


async def extract_song_info(query: str) -> dict:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return {
        "title": data.get("title", "Unknown title"),
        "url": data["url"],
        "webpage_url": data.get("webpage_url", query),
        "duration": data.get("duration"),
    }


def play_next(guild_id: int, voice_client: discord.VoiceClient) -> None:
    queue = song_queues.get(guild_id, [])
    if not queue:
        currently_playing.pop(guild_id, None)
        return
    next_song = queue.pop(0)
    currently_playing[guild_id] = next_song
    source = discord.FFmpegPCMAudio(next_song["url"], **FFMPEG_OPTIONS)
    source = discord.PCMVolumeTransformer(source, volume=1.0)
    def after_playing(error: Exception | None) -> None:
        if error:
            print(f"[player] error during playback in guild {guild_id}: {error}")
        asyncio.run_coroutine_threadsafe(_advance_and_announce(guild_id, voice_client), bot.loop)
    voice_client.play(source, after=after_playing)


async def _advance_and_announce(guild_id: int, voice_client: discord.VoiceClient) -> None:
    play_next(guild_id, voice_client)
    now = currently_playing.get(guild_id)
    channel = announce_channels.get(guild_id)
    if now and channel:
        try:
            await channel.send(f"Now playing: **{now['title']}**")
        except discord.DiscordException:
            pass


def _format_queue_lines(guild_id: int) -> list[str]:
    pending_songs = song_queues.get(guild_id, [])
    now_playing_song = currently_playing.get(guild_id)
    lines: list[str] = []
    if now_playing_song:
        lines.append(f"**Now playing:** {now_playing_song['title']}")
    for index, song in enumerate(pending_songs, start=1):
        lines.append(f"`{index}.` {song['title']} — requested by {song.get('requester', 'unknown')}")
    return lines


class PlayPositionModal(discord.ui.Modal, title="Enter queue number"):
    position = discord.ui.TextInput(label="Enter queue number", placeholder="e.g. 3", required=True, max_length=3)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            position_value = int(self.position.value)
        except ValueError:
            await interaction.response.send_message("Position must be a number.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        queue = song_queues.get(guild_id, [])
        if not queue:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        if position_value < 1 or position_value > len(queue):
            await interaction.response.send_message(
                f"Position must be between 1 and {len(queue)}.", ephemeral=True
            )
            return
        if position_value == 1:
            await interaction.response.send_message(
                f"**{queue[0]['title']}** is already next.", ephemeral=True
            )
            return
        song = queue.pop(position_value - 1)
        queue.insert(0, song)
        await interaction.response.send_message(f"Moved **{song['title']}** to the top of the queue.")


class QueueControlsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @discord.ui.button(label="Pause/Play", style=discord.ButtonStyle.success)
    async def pause_play_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        if voice_client.is_playing():
            voice_client.pause()
            await interaction.response.send_message("Paused.")
            return
        if voice_client.is_paused():
            voice_client.resume()
            await interaction.response.send_message("Resumed.")
            return
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.danger)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_playing():
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        voice_client.stop()
        await interaction.response.send_message("Skipped.")

    @discord.ui.button(label="Show Queue", style=discord.ButtonStyle.secondary)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        lines = _format_queue_lines(interaction.guild.id)
        if not lines:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Play #", style=discord.ButtonStyle.primary)
    async def play_position(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PlayPositionModal())


@bot.event
async def on_ready() -> None:
    try:
        for guild_id in INSTANT_SYNC_GUILD_IDS:
            guild = discord.Object(id=guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {guild_id}.")
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared global commands.")
    except Exception as sync_error:
        print(f"Failed to sync command tree: {sync_error}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.command(name="play", description="Search YouTube and play or queue a song.")
@app_commands.describe(query="A YouTube URL or a search term")
async def play(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    user_voice_state = interaction.user.voice
    if user_voice_state is None or user_voice_state.channel is None:
        await interaction.followup.send("You need to be in a voice channel first.", ephemeral=True)
        return
    target_channel = user_voice_state.channel
    guild_id = interaction.guild.id
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
        song = await extract_song_info(query)
    except Exception as extraction_error:
        await interaction.followup.send(f"Could not fetch that song: {extraction_error}")
        return
    song["requester"] = interaction.user.display_name
    song_queues.setdefault(guild_id, []).append(song)
    announce_channels[guild_id] = interaction.channel
    if not voice_client.is_playing() and not voice_client.is_paused():
        play_next(guild_id, voice_client)
        await interaction.followup.send(
            f"Now playing: **{song['title']}**", view=QueueControlsView()
        )
    else:
        position_in_queue = len(song_queues[guild_id])
        now_playing_song = currently_playing.get(guild_id)
        message = f"Queued **{song['title']}** at position {position_in_queue}."
        if now_playing_song:
            message += f"\nCurrently playing: **{now_playing_song['title']}**"
        await interaction.followup.send(message, view=QueueControlsView())


@bot.tree.command(name="skip", description="Skip the current song.")
async def skip(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    voice_client.stop()
    await interaction.response.send_message("Skipped.")


@bot.tree.command(name="pause", description="Pause playback.")
async def pause(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is playing to pause.", ephemeral=True)
        return
    voice_client.pause()
    await interaction.response.send_message("Paused.")


@bot.tree.command(name="resume", description="Resume paused playback.")
async def resume(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None or not voice_client.is_paused():
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    voice_client.resume()
    await interaction.response.send_message("Resumed.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    song_queues[guild_id] = []
    currently_playing.pop(guild_id, None)
    voice_client = interaction.guild.voice_client
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current song queue.")
async def queue_command(interaction: discord.Interaction) -> None:
    lines = _format_queue_lines(interaction.guild.id)
    if not lines:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    await interaction.response.send_message("\n".join(lines), view=QueueControlsView())


@bot.tree.command(name="shuffle", description="Shuffle the current queue.")
async def shuffle(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    queue = song_queues.get(guild_id, [])
    if len(queue) < 2:
        await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        return
    random.shuffle(queue)
    await interaction.response.send_message(f"Shuffled {len(queue)} songs.")


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
    removed = queue.pop(position - 1)
    await interaction.response.send_message(f"Removed **{removed['title']}** from the queue.")


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
    await interaction.response.send_message(f"Moved **{song['title']}** to the top of the queue.")


@bot.tree.command(name="nowplaying", description="Show the currently playing song.")
async def nowplaying(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    now_playing_song = currently_playing.get(guild_id)
    if now_playing_song is None:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Now playing: **{now_playing_song['title']}**\n{now_playing_song.get('webpage_url', '')}"
    )


@bot.tree.command(name="volume", description="Set playback volume (0-100).")
@app_commands.describe(level="Volume level from 0 to 100")
async def volume(interaction: discord.Interaction, level: int) -> None:
    if level < 0 or level > 100:
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    voice_client = interaction.guild.voice_client
    if voice_client is None or voice_client.source is None:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    voice_client.source.volume = level / 100
    await interaction.response.send_message(f"Volume set to {level}%.")


@bot.tree.command(name="leave", description="Disconnect the bot from voice.")
async def leave(interaction: discord.Interaction) -> None:
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    guild_id = interaction.guild.id
    song_queues.pop(guild_id, None)
    currently_playing.pop(guild_id, None)
    announce_channels.pop(guild_id, None)
    await voice_client.disconnect()
    await interaction.response.send_message("Disconnected.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(DISCORD_TOKEN)
