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

# Hard downranks: titles signaling a different version of the song
# (remixes, covers, alternate edits, commentary, live cuts).
ALT_VERSION_KEYWORDS = (
    "remix", "mashup", "bootleg", "rework",
    "sped up", "speed up", "slowed", "nightcore", "8d audio",
    "cover by", "guitar cover", "drum cover", "piano cover", "vocal cover",
    "karaoke", "instrumental",
    "reaction", "review", "tutorial", "lesson", "how to play",
    "live performance", "live at ", "live from ",
    "(live)", "[live]",
    "behind the scenes", "fan made", "fan-made",
)

# Soft downranks: video uploads of the canonical audio. Same song, but a
# `<artist> - Topic` / official-audio upload is usually preferable.
VIDEO_UPLOAD_KEYWORDS = (
    "music video", "official video", "official music video",
    "lyric video", "lyrics video",
    "(mv)", "[mv]", " mv ",
    "video edit",
)

# Past this gap, treat the candidate as a different cut (extended/remix/
# slowed) and push it below every in-tolerance match.
DURATION_MATCH_TOLERANCE_SECONDS = 25
DURATION_MATCH_TOLERANCE_FRACTION = 0.15

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


def _rerank_score(entry: dict, expected_duration: float | None) -> tuple[int, int, float]:
    """Lower is better. Components in priority order:
    1. wrong_version: 1 when expected_duration is given and this candidate's
       duration is far off — pushes alt cuts below same-length matches even
       if the title looks cleaner.
    2. keyword_score: alt-version keywords weighted heavy, video-upload
       keywords light; `<artist> - Topic` channel uploads earn a bonus.
    3. duration_diff: tiebreaker between candidates with the same keyword
       score (only meaningful when expected_duration is given)."""
    title_lower = (entry.get("title") or "").lower()
    alt_hits = sum(1 for kw in ALT_VERSION_KEYWORDS if kw in title_lower)
    video_hits = sum(1 for kw in VIDEO_UPLOAD_KEYWORDS if kw in title_lower)
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()
    topic_bonus = 3 if uploader.endswith(" - topic") else 0
    keyword_score = alt_hits * 4 + video_hits - topic_bonus

    duration_diff = 0.0
    wrong_version = 0
    candidate_duration = entry.get("duration")
    if expected_duration and candidate_duration:
        duration_diff = abs(candidate_duration - expected_duration)
        tolerance = max(
            DURATION_MATCH_TOLERANCE_SECONDS,
            expected_duration * DURATION_MATCH_TOLERANCE_FRACTION,
        )
        if duration_diff > tolerance:
            wrong_version = 1
    return (wrong_version, keyword_score, duration_diff)


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
    ranked = sorted(candidates, key=lambda e: _rerank_score(e, expected_duration))
    last_error: Exception | None = None
    for candidate in ranked:
        chosen_url = candidate.get("webpage_url") or candidate.get("url")
        if not chosen_url:
            video_id = candidate.get("id")
            if video_id:
                chosen_url = f"https://www.youtube.com/watch?v={video_id}"
        if not chosen_url:
            continue
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(None, _extract, YTDL_FORMAT_OPTIONS, chosen_url),
                timeout=EXTRACTION_TIMEOUT_SECONDS,
            )
        except Exception as candidate_error:
            last_error = candidate_error
            print(f"[audio] candidate failed for '{query}' ({type(candidate_error).__name__}: {candidate_error}); trying next")
            continue
        return _entry_to_song(data, chosen_url)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"No usable YouTube candidate for: {query}")


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
