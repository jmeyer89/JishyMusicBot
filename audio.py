import asyncio
from pathlib import Path

import yt_dlp

from config import EXTRACTION_TIMEOUT_SECONDS, MAX_QUEUE_LENGTH, YOUTUBE_COOKIES_FILE


YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "skip_download": True,
    "extractor_args": {
        "youtube": {
            # web_embedded / tv_simply / mweb act as fallbacks for age-restricted videos.
            "player_client": ["android", "tv", "web", "web_embedded", "tv_simply", "mweb"],
        }
    },
}

if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file():
    YTDL_FORMAT_OPTIONS["cookiefile"] = YOUTUBE_COOKIES_FILE
elif YOUTUBE_COOKIES_FILE:
    print(f"[audio] YOUTUBE_COOKIES_FILE set but not found: {YOUTUBE_COOKIES_FILE}")

YTDL_PLAYLIST_OPTIONS = {
    **YTDL_FORMAT_OPTIONS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
}

YTDL_SEARCH_OPTIONS = {
    **YTDL_FORMAT_OPTIONS,
    "extract_flat": "in_playlist",
}

# Title substrings that mark a result as a video upload rather than the audio
# track. Each hit adds to a candidate's penalty; lowest score wins.
VIDEO_DOWNRANK_KEYWORDS = (
    "music video",
    "official video",
    "official music video",
    "lyric video",
    "lyrics video",
    "(mv)",
    "[mv]",
    " mv ",
    "live performance",
    "live at ",
    "behind the scenes",
    "video edit",
    "fan made",
)

SEARCH_CANDIDATES = 5


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


def _extract(options: dict, query: str) -> dict:
    with yt_dlp.YoutubeDL(options) as ytdl:
        return ytdl.extract_info(query, download=False)


def _rerank_score(entry: dict, expected_duration: float | None) -> tuple[int, float]:
    """Lower is better. First component counts video-keyword hits in the title;
    second is how far the candidate's duration is from the expected one (used
    as a tiebreaker — only applied when we have a target duration)."""
    title_lower = (entry.get("title") or "").lower()
    keyword_hits = sum(1 for kw in VIDEO_DOWNRANK_KEYWORDS if kw in title_lower)
    duration_diff = 0.0
    candidate_duration = entry.get("duration")
    if expected_duration and candidate_duration:
        duration_diff = abs(candidate_duration - expected_duration)
    return (keyword_hits, duration_diff)


def _entry_to_song(data: dict, fallback_url: str) -> dict:
    if "entries" in data:
        data = data["entries"][0]
    return {
        "title": data.get("title", "Unknown title"),
        "url": data["url"],
        "webpage_url": data.get("webpage_url", fallback_url),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
    }


async def extract_song_info(query: str, expected_duration: float | None = None) -> dict:
    loop = asyncio.get_running_loop()
    if query.startswith(("http://", "https://")):
        data = await asyncio.wait_for(
            loop.run_in_executor(None, _extract, YTDL_FORMAT_OPTIONS, query),
            timeout=EXTRACTION_TIMEOUT_SECONDS,
        )
        return _entry_to_song(data, query)
    # Search path: pull N cheap metadata candidates, downrank video uploads,
    # then fully extract only the winner.
    flat = await asyncio.wait_for(
        loop.run_in_executor(None, _extract, YTDL_SEARCH_OPTIONS, f"ytsearch{SEARCH_CANDIDATES}:{query}"),
        timeout=EXTRACTION_TIMEOUT_SECONDS,
    )
    candidates = [entry for entry in (flat.get("entries") or []) if entry]
    if not candidates:
        raise RuntimeError(f"No YouTube results for: {query}")
    best = min(candidates, key=lambda e: _rerank_score(e, expected_duration))
    chosen_url = best.get("webpage_url") or best.get("url")
    if not chosen_url:
        video_id = best.get("id")
        if video_id:
            chosen_url = f"https://www.youtube.com/watch?v={video_id}"
    if not chosen_url:
        raise RuntimeError("Top YouTube candidate had no usable URL.")
    data = await asyncio.wait_for(
        loop.run_in_executor(None, _extract, YTDL_FORMAT_OPTIONS, chosen_url),
        timeout=EXTRACTION_TIMEOUT_SECONDS,
    )
    return _entry_to_song(data, chosen_url)


async def extract_playlist_info(query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    data = await asyncio.wait_for(
        loop.run_in_executor(None, _extract, YTDL_PLAYLIST_OPTIONS, query),
        timeout=EXTRACTION_TIMEOUT_SECONDS,
    )
    entries = (data.get("entries") or [])[:MAX_QUEUE_LENGTH]
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
