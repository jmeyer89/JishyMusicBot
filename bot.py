import os  # access environment variables
import asyncio  # schedule coroutines from sync callbacks
import discord  # core discord.py library
from discord import app_commands  # slash command decorators and types
from discord.ext import commands  # Bot class (still useful as the client base)
import yt_dlp  # YouTube audio extraction backend
from dotenv import load_dotenv  # load bot token from a .env file

load_dotenv()  # read the .env file into os.environ
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # pull the bot token out of the environment

# Discord intents — message_content is not strictly required for slash commands,
# but voice_states is needed so the bot can see which channel a user is in.
intents = discord.Intents.default()  # start from the sensible defaults
intents.message_content = True  # harmless to leave on; useful if prefix commands are ever re-added
intents.voice_states = True  # required to detect user voice channel membership

# We still use commands.Bot because it gives us a ready-made client + tree wrapper.
bot = commands.Bot(command_prefix="!", intents=intents)  # prefix is unused but required by the class

# Per-server song queue: { guild_id: [ {title, url, webpage_url, requester}, ... ] }
song_queues: dict[int, list[dict]] = {}  # keyed by guild ID so servers stay isolated

# Track the currently playing song per guild for /nowplaying.
currently_playing: dict[int, dict] = {}  # keyed by guild ID

# Track the text channel where /play was last used per guild, so play_next can announce songs.
announce_channels: dict[int, discord.abc.Messageable] = {}  # keyed by guild ID

# yt-dlp options — stream only, no downloads, grab the best audio URL.
YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",  # pick the best audio-only stream available
    "noplaylist": True,  # a single /play call should queue one item, not an entire playlist
    "quiet": True,  # suppress yt-dlp console spam
    "no_warnings": True,  # also suppress warnings for cleaner logs
    "default_search": "ytsearch",  # allow raw search terms, not just URLs
    "source_address": "0.0.0.0",  # bind to all interfaces to sidestep some IPv6 issues
    "skip_download": True,  # we only want the stream URL, never a local file
}

# FFmpeg options — the before_options handle dropped connections mid-stream.
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",  # survive transient network blips
    "options": "-vn",  # disable the video track, we're only playing audio
}

# Shared yt-dlp instance — cheaper than re-instantiating per call.
ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)  # reused across all /play invocations


async def extract_song_info(query: str) -> dict:
    """Run the blocking yt-dlp extraction in a thread and return a song dict."""
    loop = asyncio.get_running_loop()  # need the current loop to offload work
    # run_in_executor avoids blocking the event loop during the network-heavy extraction
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    # When a search term is used, yt-dlp returns a playlist-like dict with 'entries'.
    if "entries" in data:  # search result — take the first hit
        data = data["entries"][0]  # use the top match like a normal YouTube search
    return {
        "title": data.get("title", "Unknown title"),  # human-readable name for queue/nowplaying
        "url": data["url"],  # direct audio stream URL that FFmpeg will read
        "webpage_url": data.get("webpage_url", query),  # original YouTube link for reference
        "duration": data.get("duration"),  # track length in seconds (may be None for livestreams)
    }


def play_next(guild_id: int, voice_client: discord.VoiceClient) -> None:
    """Advance the per-guild queue and start the next song if one exists."""
    queue = song_queues.get(guild_id, [])  # fetch this guild's queue (may be empty)
    if not queue:  # nothing left to play
        currently_playing.pop(guild_id, None)  # clear the now-playing slot
        return  # leave the voice client idle; /leave or /stop will disconnect it later
    next_song = queue.pop(0)  # FIFO: pull the oldest queued item
    currently_playing[guild_id] = next_song  # remember it for /nowplaying
    # Build the audio source from the pre-extracted stream URL.
    source = discord.FFmpegPCMAudio(next_song["url"], **FFMPEG_OPTIONS)  # pipes audio through FFmpeg
    source = discord.PCMVolumeTransformer(source, volume=1.0)  # wrap so /volume can adjust playback
    # The after callback fires when the song ends (or errors); it hops back onto the loop to continue.
    def after_playing(error: Exception | None) -> None:
        if error:  # log but don't crash — usually a transient FFmpeg/network issue
            print(f"[player] error during playback in guild {guild_id}: {error}")
        # Schedule the next advance on the bot's event loop (we're on a worker thread here).
        asyncio.run_coroutine_threadsafe(_advance_and_announce(guild_id, voice_client), bot.loop)
    voice_client.play(source, after=after_playing)  # kick off playback


async def _advance_and_announce(guild_id: int, voice_client: discord.VoiceClient) -> None:
    """Coroutine wrapper around play_next that also announces the next track in the text channel."""
    play_next(guild_id, voice_client)  # start the next song (if any)
    now = currently_playing.get(guild_id)  # see what (if anything) just started
    channel = announce_channels.get(guild_id)  # where to announce it
    if now and channel:  # only announce if a song actually started
        try:
            await channel.send(f"Now playing: **{now['title']}**")  # friendly heads-up message
        except discord.DiscordException:  # channel might be gone or missing perms
            pass  # silently ignore announcement failures


@bot.event
async def on_ready() -> None:
    """Fires once when the bot connects — sync the slash command tree with Discord."""
    try:
        synced = await bot.tree.sync()  # push command definitions up to Discord
        print(f"Synced {len(synced)} slash command(s).")  # confirm what got registered
    except Exception as sync_error:  # surface sync failures without crashing the bot
        print(f"Failed to sync command tree: {sync_error}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")  # easy sanity check in the console


@bot.tree.command(name="play", description="Search YouTube and play or queue a song.")
@app_commands.describe(query="A YouTube URL or a search term")
async def play(interaction: discord.Interaction, query: str) -> None:
    """/play — join the caller's voice channel and play/queue the requested track."""
    # yt-dlp extraction can easily exceed the 3-second interaction response window, so defer up front.
    await interaction.response.defer()  # tells Discord "working on it" and lets us followup later
    user_voice_state = interaction.user.voice  # where the requester is currently sitting
    if user_voice_state is None or user_voice_state.channel is None:  # must be in a voice channel
        await interaction.followup.send("You need to be in a voice channel first.", ephemeral=True)
        return
    target_channel = user_voice_state.channel  # the voice channel we need to be in
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    if voice_client is None:  # not connected yet — join the user's channel
        voice_client = await target_channel.connect()
    elif voice_client.channel != target_channel:  # connected to a different channel — move over
        await voice_client.move_to(target_channel)
    try:
        song = await extract_song_info(query)  # resolve title + stream URL via yt-dlp
    except Exception as extraction_error:  # invalid query, age-gate, region-lock, etc.
        await interaction.followup.send(f"Could not fetch that song: {extraction_error}")
        return
    song["requester"] = interaction.user.display_name  # stamp who requested it for queue display
    guild_id = interaction.guild.id  # key for all the per-server state
    song_queues.setdefault(guild_id, []).append(song)  # enqueue; create list on first use
    announce_channels[guild_id] = interaction.channel  # remember where to post auto-advance messages
    if not voice_client.is_playing() and not voice_client.is_paused():  # idle — start immediately
        play_next(guild_id, voice_client)  # pulls the song we just queued and plays it
        await interaction.followup.send(f"Now playing: **{song['title']}**")  # confirm to the user
    else:  # something is already playing — just tell them their position in line
        position_in_queue = len(song_queues[guild_id])  # 1-based position from the user's POV
        await interaction.followup.send(f"Queued **{song['title']}** at position {position_in_queue}.")


@bot.tree.command(name="skip", description="Skip the current song.")
async def skip(interaction: discord.Interaction) -> None:
    """/skip — stop the current track so the after-callback advances the queue."""
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    if voice_client is None or not voice_client.is_playing():  # nothing to skip
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    voice_client.stop()  # triggers the after=after_playing callback, which advances the queue
    await interaction.response.send_message("Skipped.")  # acknowledge to the user


@bot.tree.command(name="pause", description="Pause playback.")
async def pause(interaction: discord.Interaction) -> None:
    """/pause — freeze the current track in place."""
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    if voice_client is None or not voice_client.is_playing():  # nothing actively playing
        await interaction.response.send_message("Nothing is playing to pause.", ephemeral=True)
        return
    voice_client.pause()  # halt the audio stream; can be resumed later
    await interaction.response.send_message("Paused.")  # confirm to the user


@bot.tree.command(name="resume", description="Resume paused playback.")
async def resume(interaction: discord.Interaction) -> None:
    """/resume — continue a previously paused track."""
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    if voice_client is None or not voice_client.is_paused():  # only valid from a paused state
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    voice_client.resume()  # pick up where pause() left off
    await interaction.response.send_message("Resumed.")  # confirm to the user


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction) -> None:
    """/stop — wipe the queue and halt whatever is currently playing."""
    guild_id = interaction.guild.id  # key for the per-server queue
    song_queues[guild_id] = []  # clear any pending songs so the after-callback has nothing to advance to
    currently_playing.pop(guild_id, None)  # clear the now-playing record
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()  # halt audio; with an empty queue, play_next will no-op
    await interaction.response.send_message("Stopped and cleared the queue.")  # confirm to the user


@bot.tree.command(name="queue", description="Show the current song queue.")
async def queue_command(interaction: discord.Interaction) -> None:
    """/queue — print the upcoming songs in order."""
    guild_id = interaction.guild.id  # key for the per-server queue
    pending_songs = song_queues.get(guild_id, [])  # songs waiting to be played
    now_playing_song = currently_playing.get(guild_id)  # may be None if idle
    if not pending_songs and not now_playing_song:  # totally empty state
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines: list[str] = []  # accumulate display rows
    if now_playing_song:  # lead with what's playing right now
        lines.append(f"**Now playing:** {now_playing_song['title']}")
    for index, song in enumerate(pending_songs, start=1):  # number the upcoming songs 1..N
        lines.append(f"`{index}.` {song['title']} — requested by {song.get('requester', 'unknown')}")
    await interaction.response.send_message("\n".join(lines))  # send everything as one message


@bot.tree.command(name="nowplaying", description="Show the currently playing song.")
async def nowplaying(interaction: discord.Interaction) -> None:
    """/nowplaying — report the track currently on the voice client."""
    guild_id = interaction.guild.id  # key for the per-server state
    now_playing_song = currently_playing.get(guild_id)  # may be None if idle
    if now_playing_song is None:  # nothing playing
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    # Include the original YouTube URL so users can click through if they want.
    await interaction.response.send_message(
        f"Now playing: **{now_playing_song['title']}**\n{now_playing_song.get('webpage_url', '')}"
    )


@bot.tree.command(name="volume", description="Set playback volume (0-100).")
@app_commands.describe(level="Volume level from 0 to 100")
async def volume(interaction: discord.Interaction, level: int) -> None:
    """/volume — scale the PCMVolumeTransformer on the active source."""
    if level < 0 or level > 100:  # guard against nonsense inputs
        await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
        return
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    # Source is only a VolumeTransformer while something is playing/paused.
    if voice_client is None or voice_client.source is None:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    # discord.py's VolumeTransformer uses a 0.0-2.0 float range; 1.0 == 100%.
    voice_client.source.volume = level / 100  # scale 0-100 down to 0.0-1.0
    await interaction.response.send_message(f"Volume set to {level}%.")  # confirm to the user


@bot.tree.command(name="leave", description="Disconnect the bot from voice.")
async def leave(interaction: discord.Interaction) -> None:
    """/leave — disconnect cleanly and clear per-guild state."""
    voice_client = interaction.guild.voice_client  # current voice connection (if any)
    if voice_client is None:  # not in a voice channel
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    guild_id = interaction.guild.id  # key for the per-server state
    song_queues.pop(guild_id, None)  # drop any queued songs
    currently_playing.pop(guild_id, None)  # drop the now-playing record
    announce_channels.pop(guild_id, None)  # forget the announcement channel
    await voice_client.disconnect()  # cleanly leave the voice channel
    await interaction.response.send_message("Disconnected.")  # confirm to the user


if __name__ == "__main__":
    if not DISCORD_TOKEN:  # fail fast with a clear message if the token is missing
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(DISCORD_TOKEN)  # connect to Discord and block forever
