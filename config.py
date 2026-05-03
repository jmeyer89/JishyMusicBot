import os
import json
import logging
import time
from collections import deque
from pathlib import Path
import discord
from dotenv import load_dotenv


load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN")

INSTANT_SYNC_GUILD_IDS = [
    int(gid.strip())
    for gid in os.getenv("INSTANT_SYNC_GUILD_IDS", "").split(",")
    if gid.strip()
]

MAX_QUEUE_LENGTH = 25
NOW_PLAYING_REFRESH_SECONDS = 5
DEFAULT_VOLUME = 0.5
INACTIVITY_TIMEOUT_SECONDS = 900
PAUSED_TIMEOUT_SECONDS = 900

SPAM_WINDOW_SECONDS = 5.0
SPAM_MAX_ACTIONS = 10

SETUP_COMMANDS = {"music_show_config", "music_set_channel", "music_set_role"}

_CONFIG_FILE = Path(__file__).parent / "bot_config.json"
AUDIT_LOG_FILE = Path(__file__).parent / "bot_audit.log"

guild_config: dict[int, dict] = {}


def load_config() -> None:
    global guild_config
    if not _CONFIG_FILE.exists():
        guild_config = {}
        return
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        guild_config = {int(k): v for k, v in data.items()}
    except Exception as load_error:
        print(f"[config] load failed: {load_error}")
        guild_config = {}


def save_config() -> None:
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in guild_config.items()}, f, indent=2)
    except Exception as save_error:
        print(f"[config] save failed: {save_error}")


_audit_logger = logging.getLogger("jishybot.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
if not _audit_logger.handlers:
    _audit_handler = logging.FileHandler(AUDIT_LOG_FILE, encoding="utf-8")
    _audit_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _audit_logger.addHandler(_audit_handler)


def audit(interaction: discord.Interaction, action: str) -> None:
    try:
        guild_name = interaction.guild.name if interaction.guild else "DM"
        channel_name = getattr(interaction.channel, "name", None) or str(interaction.channel_id)
        user = f"{interaction.user}({interaction.user.id})"
        _audit_logger.info(f"{guild_name} #{channel_name} | {user} | {action}")
    except Exception:
        pass


_user_actions: dict[int, deque[float]] = {}


def check_spam(user_id: int) -> bool:
    now = time.monotonic()
    history = _user_actions.setdefault(user_id, deque())
    while history and now - history[0] > SPAM_WINDOW_SECONDS:
        history.popleft()
    if len(history) >= SPAM_MAX_ACTIONS:
        return False
    history.append(now)
    return True


async def enforce_spam_guard(interaction: discord.Interaction, label: str) -> bool:
    if check_spam(interaction.user.id):
        return True
    audit(interaction, f"RATE_LIMITED {label}")
    try:
        await interaction.response.send_message(
            "Slow down — too many actions. Try again in a few seconds.",
            ephemeral=True,
        )
    except (discord.InteractionResponded, discord.HTTPException):
        pass
    return False


def is_interaction_allowed(interaction: discord.Interaction) -> tuple[bool, str | None]:
    if interaction.guild is None:
        return False, "This bot only works in servers, not in DMs."
    cfg = guild_config.get(interaction.guild.id, {})
    allowed_channels = cfg.get("allowed_channels") or []
    if allowed_channels and interaction.channel_id not in allowed_channels:
        return False, "This bot is restricted to a different channel."
    allowed_role = cfg.get("allowed_role")
    if allowed_role:
        member = interaction.user
        if not isinstance(member, discord.Member) or not any(r.id == allowed_role for r in member.roles):
            return False, "You don't have the role required to use this bot."
    return True, None
