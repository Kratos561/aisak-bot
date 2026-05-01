from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _get_color(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value, 16) if raw_value.lower().startswith("0x") else int(raw_value)
    except ValueError:
        return default


def _get_test_guild_ids() -> list[int]:
    raw_value = os.getenv("TEST_GUILD_IDS", "")
    guild_ids: list[int] = []
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            guild_ids.append(int(item))
        except ValueError:
            continue
    return guild_ids


def _get_csv(name: str, default: str) -> list[str]:
    raw_value = os.getenv(name, default)
    return [item.strip() for item in raw_value.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    discord_token: str | None = field(default_factory=lambda: os.getenv("DISCORD_TOKEN"))
    spotify_client_id: str | None = field(default_factory=lambda: os.getenv("SPOTIFY_CLIENT_ID"))
    spotify_client_secret: str | None = field(default_factory=lambda: os.getenv("SPOTIFY_CLIENT_SECRET"))
    bot_prefix: str = field(default_factory=lambda: os.getenv("BOT_PREFIX", "!"))
    bot_color: int = field(default_factory=lambda: _get_color("BOT_COLOR", 0x1F77B4))
    default_volume: int = field(default_factory=lambda: _get_int("DEFAULT_VOLUME", 70))
    max_queue_length: int = field(default_factory=lambda: _get_int("MAX_QUEUE_LENGTH", 100))
    inactivity_timeout: int = field(default_factory=lambda: _get_int("INACTIVITY_TIMEOUT", 300))
    discord_retry_delay: int = field(default_factory=lambda: _get_int("DISCORD_RETRY_DELAY", 20))
    flask_host: str = field(default_factory=lambda: os.getenv("FLASK_HOST", "0.0.0.0"))
    flask_port: int = field(default_factory=lambda: _get_int("PORT", _get_int("FLASK_PORT", 7860)))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "./data/logs/aisak.log"))
    support_url: str = field(default_factory=lambda: os.getenv("SUPPORT_URL", "https://huggingface.co"))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "UTC"))
    lyrics_endpoint: str = field(default_factory=lambda: os.getenv("LYRICS_ENDPOINT", "https://api.lyrics.ovh/v1"))
    spotify_playlist_limit: int = field(default_factory=lambda: _get_int("SPOTIFY_PLAYLIST_LIMIT", 20))
    ytdlp_youtube_player_clients: list[str] = field(
        default_factory=lambda: _get_csv("YTDLP_YOUTUBE_PLAYER_CLIENTS", "mweb,web_safari")
    )
    ytdlp_youtube_retry_player_clients: list[str] = field(
        default_factory=lambda: _get_csv("YTDLP_YOUTUBE_RETRY_PLAYER_CLIENTS", "web_safari,mweb,web_embedded")
    )
    ytdlp_js_runtimes: list[str] = field(default_factory=lambda: _get_csv("YTDLP_JS_RUNTIMES", "node"))
    ytdlp_remote_components: list[str] = field(default_factory=lambda: _get_csv("YTDLP_REMOTE_COMPONENTS", "ejs:github"))
    ytdlp_bgutil_base_url: str = field(default_factory=lambda: os.getenv("YTDLP_BGUTIL_BASE_URL", "http://127.0.0.1:4416"))
    ytdlp_bgutil_server_home: str | None = field(default_factory=lambda: os.getenv("YTDLP_BGUTIL_SERVER_HOME"))
    ytdlp_search_limit: int = field(default_factory=lambda: _get_int("YTDLP_SEARCH_LIMIT", 5))
    ytdlp_operation_timeout: int = field(default_factory=lambda: _get_int("YTDLP_OPERATION_TIMEOUT", 35))
    play_candidate_limit: int = field(default_factory=lambda: _get_int("PLAY_CANDIDATE_LIMIT", 8))
    test_guild_ids: list[int] = field(default_factory=_get_test_guild_ids)

    @property
    def default_volume_ratio(self) -> float:
        return max(0.0, min(self.default_volume, 100)) / 100


def get_settings() -> Settings:
    return Settings()
