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
TEST_GUILD_ID = 205409317283823627

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

song_queues: dict[int, list[dict]] = {}
currently_playing: dict[int, dict] = {}
announce_channels: dict[int, discord.abc.Messageable] = {}

YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "skip_download": True,
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


@bot.event
async def on_ready() -> None:
    try:
        guild = discord.Object(id=TEST_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash command(s) to test guild.")
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
    guild_id = interaction.guild.id
    song_queues.setdefault(guild_id, []).append(song)
    announce_channels[guild_id] = interaction.channel
    if not voice_client.is_playing() and not voice_client.is_paused():
        play_next(guild_id, voice_client)
        await interaction.followup.send(f"Now playing: **{song['title']}**")
    else:
        position_in_queue = len(song_queues[guild_id])
        await interaction.followup.send(f"Queued **{song['title']}** at position {position_in_queue}.")


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
    guild_id = interaction.guild.id
    pending_songs = song_queues.get(guild_id, [])
    now_playing_song = currently_playing.get(guild_id)
    if not pending_songs and not now_playing_song:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines: list[str] = []
    if now_playing_song:
        lines.append(f"**Now playing:** {now_playing_song['title']}")
    for index, song in enumerate(pending_songs, start=1):
        lines.append(f"`{index}.` {song['title']} — requested by {song.get('requester', 'unknown')}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="shuffle", description="Shuffle the current queue.")
async def shuffle(interaction: discord.Interaction) -> None:
    guild_id = interaction.guild.id
    queue = song_queues.get(guild_id, [])
    if len(queue) < 2:
        await interaction.response.send_message("Not enough songs in the queue to shuffle.", ephemeral=True)
        return
    random.shuffle(queue)
    await interaction.response.send_message(f"Shuffled {len(queue)} songs.")


@bot.tree.command(name="remove", description="Remove a song from the queue by its position.")
@app_commands.describe(position="Queue position to remove (see /queue)")
async def remove(interaction: discord.Interaction, position: int) -> None:
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
