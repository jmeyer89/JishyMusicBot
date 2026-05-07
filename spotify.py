import asyncio
import base64
import re
import time

import requests

from config import MAX_QUEUE_LENGTH, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

_access_token: str | None = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()

_URL_RE = re.compile(
    r"(?:https?://(?:open\.)?spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist)/([A-Za-z0-9]+)"
    r"|spotify:(track|album|playlist):([A-Za-z0-9]+))"
)


class SpotifyError(Exception):
    pass


def is_spotify_url(query: str) -> bool:
    return bool(_URL_RE.search(query))


def parse_spotify_url(query: str) -> tuple[str, str] | None:
    match = _URL_RE.search(query)
    if not match:
        return None
    kind = match.group(1) or match.group(3)
    spotify_id = match.group(2) or match.group(4)
    return kind, spotify_id


def _fetch_client_credentials_token() -> dict:
    auth_header = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    response = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {auth_header}"},
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


async def _get_access_token() -> str:
    global _access_token, _token_expires_at
    async with _token_lock:
        if _access_token and time.monotonic() < _token_expires_at - 30:
            return _access_token
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            raise SpotifyError(
                "Spotify is not configured. Add SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET to your .env file."
            )
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, _fetch_client_credentials_token)
        except requests.RequestException as exc:
            raise SpotifyError(f"Could not get Spotify access token: {exc}") from exc
        _access_token = data["access_token"]
        _token_expires_at = time.monotonic() + data.get("expires_in", 3600)
        return _access_token


async def _get_json(url: str, params: dict | None = None) -> dict:
    token = await _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: requests.get(url, headers=headers, params=params, timeout=10),
        )
    except requests.RequestException as exc:
        raise SpotifyError(f"Spotify request failed: {exc}") from exc
    if not response.ok:
        message = _extract_error_message(response) or response.reason or "Spotify request failed."
        raise SpotifyError(message)
    try:
        return response.json()
    except ValueError as exc:
        raise SpotifyError(f"Spotify returned invalid JSON: {exc}") from exc


def _extract_error_message(response: requests.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message")
    if isinstance(error, str):
        return error
    return None


def _track_to_entry(track: dict, fallback_thumb: str | None = None) -> dict | None:
    if not track or track.get("is_local"):
        return None
    name = track.get("name") or "Unknown title"
    artists = [a.get("name", "") for a in track.get("artists") or [] if a.get("name")]
    artist_str = ", ".join(artists)
    title = f"{artist_str} - {name}" if artist_str else name
    duration_ms = track.get("duration_ms") or 0
    duration = duration_ms // 1000 if duration_ms else None
    webpage_url = (track.get("external_urls") or {}).get("spotify")
    images = (track.get("album") or {}).get("images") or []
    thumb = images[0].get("url") if images else fallback_thumb
    return {
        "title": title,
        "url": None,
        "webpage_url": webpage_url,
        "duration": duration,
        "thumbnail": thumb,
        "search_query": title,
    }


async def search_tracks(query: str, limit: int = 10) -> list[dict]:
    data = await _get_json(
        f"{API_BASE}/search",
        params={"q": query, "type": "track", "limit": limit},
    )
    items = (data.get("tracks") or {}).get("items") or []
    return [entry for entry in (_track_to_entry(t) for t in items) if entry]


async def extract_spotify_tracks(query: str) -> list[dict]:
    parsed = parse_spotify_url(query)
    if parsed is None:
        return []
    kind, spotify_id = parsed

    if kind == "track":
        data = await _get_json(f"{API_BASE}/tracks/{spotify_id}")
        entry = _track_to_entry(data)
        return [entry] if entry else []

    if kind == "album":
        album = await _get_json(f"{API_BASE}/albums/{spotify_id}")
        images = album.get("images") or []
        thumb = images[0].get("url") if images else None
        items = (album.get("tracks") or {}).get("items") or []
        entries: list[dict] = []
        for track in items:
            track.setdefault("album", {"images": images})
            entry = _track_to_entry(track, fallback_thumb=thumb)
            if entry:
                entries.append(entry)
                if len(entries) >= MAX_QUEUE_LENGTH:
                    return entries
        next_url = (album.get("tracks") or {}).get("next")
        while next_url:
            page = await _get_json(next_url)
            for track in page.get("items") or []:
                track.setdefault("album", {"images": images})
                entry = _track_to_entry(track, fallback_thumb=thumb)
                if entry:
                    entries.append(entry)
                    if len(entries) >= MAX_QUEUE_LENGTH:
                        return entries
            next_url = page.get("next")
        return entries

    if kind == "playlist":
        url = f"{API_BASE}/playlists/{spotify_id}/tracks"
        params: dict | None = {"limit": 100}
        entries = []
        while url:
            page = await _get_json(url, params=params)
            for item in page.get("items") or []:
                track = item.get("track") or {}
                if track.get("type") != "track":
                    continue
                entry = _track_to_entry(track)
                if entry:
                    entries.append(entry)
                    if len(entries) >= MAX_QUEUE_LENGTH:
                        return entries
            url = page.get("next")
            params = None
        return entries

    return []
