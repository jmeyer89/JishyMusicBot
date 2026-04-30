import asyncio
import yt_dlp


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

YTDL_PLAYLIST_OPTIONS = {
    **YTDL_FORMAT_OPTIONS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
}

_ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)
_ytdl_playlist = yt_dlp.YoutubeDL(YTDL_PLAYLIST_OPTIONS)


def ffmpeg_options(seek: float | None = None) -> dict:
    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if seek and seek > 0:
        before = f"-ss {seek:.3f} {before}"
    return {
        "before_options": before,
        "options": "-vn -af dynaudnorm",
    }


def is_playlist_url(query: str) -> bool:
    return "playlist?list=" in query


async def extract_song_info(query: str) -> dict:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: _ytdl.extract_info(query, download=False))
    if "entries" in data:
        data = data["entries"][0]
    return {
        "title": data.get("title", "Unknown title"),
        "url": data["url"],
        "webpage_url": data.get("webpage_url", query),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
    }


async def extract_playlist_info(query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: _ytdl_playlist.extract_info(query, download=False))
    entries = data.get("entries") or []
    songs: list[dict] = []
    for entry in entries:
        if not entry:
            continue
        webpage_url = entry.get("url") or entry.get("webpage_url")
        if not webpage_url:
            video_id = entry.get("id")
            if video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        if not webpage_url:
            continue
        thumbs = entry.get("thumbnails") or []
        songs.append({
            "title": entry.get("title", "Unknown title"),
            "url": None,
            "webpage_url": webpage_url,
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None),
        })
    return songs
