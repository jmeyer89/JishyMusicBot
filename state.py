import asyncio
import discord


song_queues: dict[int, list[dict]] = {}
currently_playing: dict[int, dict] = {}
saved_queues: dict[int, list[dict]] = {}

announce_channels: dict[int, discord.abc.Messageable] = {}
now_playing_messages: dict[int, discord.Message] = {}
now_playing_tasks: dict[int, asyncio.Task] = {}
inactivity_tasks: dict[int, asyncio.Task] = {}
play_locks: dict[int, asyncio.Lock] = {}

panel_writer_tasks: dict[int, asyncio.Task] = {}
panel_update_events: dict[int, asyncio.Event] = {}
panel_last_edit_at: dict[int, float] = {}

queue_expanded: dict[int, bool] = {}
volume_levels: dict[int, float] = {}
search_alternatives: dict[int, list[dict]] = {}
