"""utils/models.py — Estructuras de datos del bot."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import discord
import wavelink


class RepeatMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


class FilterPreset(str, Enum):
    OFF = "off"
    BASSBOOST = "bassboost"
    CLEAR = "clear"
    RADIO = "radio"
    NIGHTCORE = "nightcore"
    VAPORWAVE = "vaporwave"


@dataclass(slots=True)
class Track:
    """Cancion individual. El campo `_playable` guarda el Playable de
    wavelink cacheado para no tener que re-resolverlo.
    """
    title: str
    webpage_url: str
    duration: int | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    search_query: str | None = None
    requester_id: int | None = None
    requester_name: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    # Si el Track viene del fallback yt-dlp, esta URL es el stream directo
    # de googlevideo (caduca en ~6h). Si no, es None y se resuelve via
    # Lavalink normalmente.
    stream_url: str | None = None
    _playable: Any | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class MessageRef:
    channel_id: int
    message_id: int


@dataclass(slots=True)
class GuildMusicState:
    """Estado de musica por servidor. Una instancia por guild."""
    guild_id: int
    queue: deque[Track] = field(default_factory=deque)
    history: deque[Track] = field(default_factory=lambda: deque(maxlen=50))
    player: wavelink.Player | None = None
    current: Track | None = None
    volume: float = 0.7
    playback_speed: float = 1.0
    pitch_semitones: int = 0
    filter_preset: FilterPreset = FilterPreset.OFF
    repeat_mode: RepeatMode = RepeatMode.OFF
    text_channel_id: int | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    paused_seconds: float = 0.0
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    play_lock_owner: asyncio.Task | None = None
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    auto_disconnect_task: asyncio.Task[None] | None = None
    autoplay_enabled: bool = False
    manual_skip: bool = False
    manual_stop: bool = False
    active_panel: MessageRef | None = None

    def reset_progress(self) -> None:
        self.started_at = datetime.now(UTC)
        self.paused_at = None
        self.paused_seconds = 0.0

    def clear_progress(self) -> None:
        self.started_at = None
        self.paused_at = None
        self.paused_seconds = 0.0

    def elapsed_seconds(self) -> int:
        if self.started_at is None:
            return 0
        anchor = self.paused_at or datetime.now(UTC)
        return max(0, int((anchor - self.started_at).total_seconds() - self.paused_seconds))


@dataclass(slots=True)
class RuntimeStatus:
    """Estado del runtime expuesto en /health y /status."""
    service: str = "AISAK"
    bot_status: str = "starting"
    discord_gateway: str = "disconnected"
    lavalink_connected: bool = False
    detail: str = "Inicializando"
