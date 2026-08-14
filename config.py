"""config.py — Settings cargados desde env vars.

Toda la configuracion del bot vive aqui. No hay settings esparcidos por
otros modulos; si necesitas un valor nuevo, anadelo a Settings y usalo
desde ahi.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_color(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    s = raw.strip()
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        if s.startswith("#"):
            return int(s[1:], 16)
        return int(s, 16)
    except ValueError:
        return default


def _get_guild_ids() -> list[int]:
    raw = os.getenv("TEST_GUILD_IDS", "")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


@dataclass(slots=True, frozen=True)
class Settings:
    # Discord
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))

    # YouTube OAuth (cuenta burner)
    youtube_oauth_refresh_token: str = field(
        default_factory=lambda: os.getenv("YOUTUBE_OAUTH_REFRESH_TOKEN", "")
    )
    youtube_oauth_client_id: str = field(
        default_factory=lambda: os.getenv("YOUTUBE_OAUTH_CLIENT_ID", "")
    )
    youtube_oauth_client_secret: str = field(
        default_factory=lambda: os.getenv("YOUTUBE_OAUTH_CLIENT_SECRET", "")
    )

    # Guilds de prueba (sync inmediato de slash commands)
    test_guild_ids: tuple[int, ...] = field(
        default_factory=lambda: tuple(_get_guild_ids())
    )

    # Lavalink
    lavalink_host: str = field(default_factory=lambda: os.getenv("LAVALINK_HOST", "127.0.0.1"))
    lavalink_port: int = field(default_factory=lambda: _get_int("LAVALINK_PORT", 2333))
    lavalink_password: str = field(default_factory=lambda: os.getenv("LAVALINK_PASSWORD", "youshallnotpass"))

    # Bot
    bot_color: int = field(default_factory=lambda: _get_color("BOT_COLOR", 0x1F77B4))
    default_volume: int = field(default_factory=lambda: _get_int("DEFAULT_VOLUME", 70))
    max_queue_length: int = field(default_factory=lambda: _get_int("MAX_QUEUE_LENGTH", 100))
    inactivity_timeout: int = field(default_factory=lambda: _get_int("INACTIVITY_TIMEOUT", 300))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())

    # Web server (Render expone PORT=10000 por defecto para web services)
    web_port: int = field(default_factory=lambda: _get_int("PORT", _get_int("WEB_PORT", 10000)))

    # Tuning de extraccion de audio
    search_limit: int = field(default_factory=lambda: _get_int("SEARCH_LIMIT", 5))
    extract_timeout: int = field(default_factory=lambda: _get_int("EXTRACT_TIMEOUT", 25))
    breaker_threshold: int = field(default_factory=lambda: _get_int("BREAKER_THRESHOLD", 3))
    breaker_cooldown: int = field(default_factory=lambda: _get_int("BREAKER_COOLDOWN", 60))

    @property
    def default_volume_ratio(self) -> float:
        return max(0.0, min(self.default_volume, 100)) / 100.0

    @property
    def oauth_configured(self) -> bool:
        return bool(
            self.youtube_oauth_refresh_token
            and self.youtube_oauth_client_id
            and self.youtube_oauth_client_secret
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings