from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

import discord


class RepeatMode(str, Enum):
    OFF = "off"
    ONE = "one"
    ALL = "all"


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str | None = None
    stream_headers: dict[str, str] = field(default_factory=dict)
    duration: int | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    source: str = "yt-dlp"
    search_query: str | None = None
    requester_id: int | None = None
    requester_name: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(slots=True)
class MessageRef:
    channel_id: int
    message_id: int


@dataclass(slots=True)
class GuildMusicState:
    guild_id: int
    queue: deque[Track] = field(default_factory=deque)
    history: deque[Track] = field(default_factory=lambda: deque(maxlen=50))
    voice_client: discord.VoiceClient | None = None
    current: Track | None = None
    volume: float = 0.7
    repeat_mode: RepeatMode = RepeatMode.OFF
    text_channel_id: int | None = None
    started_at: datetime | None = None
    paused_at: datetime | None = None
    paused_seconds: float = 0.0
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    auto_disconnect_task: asyncio.Task[None] | None = None
    autoplay_enabled: bool = False
    manual_skip: bool = False
    manual_stop: bool = False
    active_panel: MessageRef | None = None
    active_panel_track_id: str | None = None
    track_messages: dict[str, MessageRef] = field(default_factory=dict)

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
