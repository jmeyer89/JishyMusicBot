import json
import time
from pathlib import Path


_PLAYLISTS_FILE = Path(__file__).parent / "saved_queues.json"

PLAYLIST_NAME_MAX = 32
MAX_PLAYLISTS_PER_GUILD = 25
_FIELDS_TO_KEEP = ("title", "webpage_url", "search_query", "duration", "thumbnail")

# {guild_id: {normalized_name: {"songs": [...], "saved_by": "...", "saved_at": <epoch>, "display_name": "..."}}}
playlists: dict[int, dict[str, dict]] = {}


def load_playlists() -> None:
    global playlists
    if not _PLAYLISTS_FILE.exists():
        playlists = {}
        return
    try:
        with open(_PLAYLISTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        playlists = {int(k): v for k, v in data.items()}
    except Exception as load_error:
        print(f"[playlists] load failed: {load_error}")
        playlists = {}


def save_playlists_to_disk() -> None:
    try:
        with open(_PLAYLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in playlists.items()}, f, indent=2)
    except Exception as save_error:
        print(f"[playlists] save failed: {save_error}")


def _strip_song(song: dict) -> dict:
    return {k: song.get(k) for k in _FIELDS_TO_KEEP if song.get(k) is not None}


def _normalize(name: str) -> str:
    return name.strip().lower()[:PLAYLIST_NAME_MAX]


def save_queue(
    guild_id: int,
    name: str,
    songs: list[dict],
    requester: str,
) -> tuple[bool, str]:
    """Returns (success, user_facing_message)."""
    normalized = _normalize(name)
    if not normalized:
        return False, "Playlist name cannot be empty."
    if not songs:
        return False, "Nothing to save — the queue is empty."
    guild_playlists = playlists.setdefault(guild_id, {})
    is_new = normalized not in guild_playlists
    if is_new and len(guild_playlists) >= MAX_PLAYLISTS_PER_GUILD:
        return False, f"Maximum saved playlists reached ({MAX_PLAYLISTS_PER_GUILD}). Delete one first."
    guild_playlists[normalized] = {
        "songs": [_strip_song(s) for s in songs],
        "saved_by": requester,
        "saved_at": time.time(),
        "display_name": name.strip()[:PLAYLIST_NAME_MAX],
    }
    save_playlists_to_disk()
    verb = "Saved" if is_new else "Updated"
    return True, f"{verb} **{name.strip()}** with {len(songs)} song(s)."


def create_playlist(guild_id: int, name: str, requester: str) -> tuple[bool, str]:
    """Create a new empty playlist. Returns (success, user_facing_message)."""
    normalized = _normalize(name)
    if not normalized:
        return False, "Playlist name cannot be empty."
    guild_playlists = playlists.setdefault(guild_id, {})
    if normalized in guild_playlists:
        return False, f"Playlist **{name.strip()}** already exists."
    if len(guild_playlists) >= MAX_PLAYLISTS_PER_GUILD:
        return False, f"Maximum saved playlists reached ({MAX_PLAYLISTS_PER_GUILD}). Delete one first."
    guild_playlists[normalized] = {
        "songs": [],
        "saved_by": requester,
        "saved_at": time.time(),
        "display_name": name.strip()[:PLAYLIST_NAME_MAX],
    }
    save_playlists_to_disk()
    return True, f"Created empty playlist **{name.strip()}**."


def add_song_to_playlist(
    guild_id: int,
    name: str,
    song: dict,
    requester: str,
) -> tuple[bool, str]:
    """Append a song to an existing playlist. Dedupes by webpage_url so the
    same song can't be added twice. Returns (success, user_facing_message)."""
    if not song:
        return False, "No song to add."
    normalized = _normalize(name)
    guild_playlists = playlists.get(guild_id, {})
    entry = guild_playlists.get(normalized)
    if entry is None:
        return False, f"No playlist named **{name}**."
    stripped = _strip_song(song)
    title = stripped.get("title") or "song"
    display = entry.get("display_name", normalized)
    existing_url = stripped.get("webpage_url")
    if existing_url:
        for existing in entry.get("songs", []):
            if existing.get("webpage_url") == existing_url:
                return False, f"**{title}** is already in **{display}**."
    entry.setdefault("songs", []).append(stripped)
    entry["saved_at"] = time.time()
    entry["saved_by"] = requester
    save_playlists_to_disk()
    return True, f"Added **{title}** to **{display}**."


def load_queue(guild_id: int, name: str) -> list[dict] | None:
    normalized = _normalize(name)
    guild_playlists = playlists.get(guild_id, {})
    entry = guild_playlists.get(normalized)
    if entry is None:
        return None
    return [dict(s) for s in entry.get("songs", [])]


def delete_queue(guild_id: int, name: str) -> bool:
    normalized = _normalize(name)
    guild_playlists = playlists.get(guild_id, {})
    if normalized not in guild_playlists:
        return False
    del guild_playlists[normalized]
    if not guild_playlists:
        playlists.pop(guild_id, None)
    save_playlists_to_disk()
    return True


def list_for_guild(guild_id: int) -> dict[str, dict]:
    return playlists.get(guild_id, {})


def names_for_guild(guild_id: int) -> list[str]:
    """Display names (for autocomplete), sorted."""
    return sorted(
        entry.get("display_name", normalized)
        for normalized, entry in playlists.get(guild_id, {}).items()
    )
