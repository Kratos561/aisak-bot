from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from cogs.controls import ControlsCog
from cogs.music import MusicCog
from cogs.search import SearchCog
from utils.audio_effects import build_ffmpeg_filter_chain, describe_audio_effects
from utils.logger import configure_logging
from utils.models import AudioFilterPreset
from utils.models import Track
from utils.music_manager import MusicManager

SAMPLE_AUDIO_PATH = ROOT / "data" / "cache" / "simulate-tone.wav"


def ensure_sample_audio_file() -> Path:
    SAMPLE_AUDIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SAMPLE_AUDIO_PATH.exists():
        return SAMPLE_AUDIO_PATH

    sample_rate = 48_000
    duration_seconds = 6
    amplitude = 11_500
    total_frames = sample_rate * duration_seconds

    with wave.open(str(SAMPLE_AUDIO_PATH), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for frame_index in range(total_frames):
            sample = int(amplitude * math.sin(2 * math.pi * 440 * frame_index / sample_rate))
            chunk = sample.to_bytes(2, "little", signed=True)
            wav_file.writeframesraw(chunk + chunk)

    return SAMPLE_AUDIO_PATH


class FakeResponse:
    def __init__(self, interaction: "FakeInteraction", sink: list[dict[str, Any]]) -> None:
        self._interaction = interaction
        self._done = False
        self._sink = sink

    async def defer(self, thinking: bool = False, ephemeral: bool = False) -> None:
        self._done = True
        self._sink.append({"kind": "defer", "thinking": thinking, "ephemeral": ephemeral})

    async def send_message(
        self,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
    ) -> None:
        self._done = True
        message = self._interaction.channel.create_message(content=content, embed=embed, ephemeral=ephemeral, view=view)
        self._interaction._original_response = message
        self._sink.append(
            {
                "kind": "response",
                "content": content,
                "embed": embed,
                "ephemeral": ephemeral,
                "view": type(view).__name__ if view else None,
                "message_id": message.id,
            }
        )

    def is_done(self) -> bool:
        return self._done


class FakeFollowup:
    def __init__(self, interaction: "FakeInteraction", sink: list[dict[str, Any]]) -> None:
        self._interaction = interaction
        self._sink = sink

    async def send(
        self,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
        wait: bool = False,
    ) -> None:
        message = self._interaction.channel.create_message(content=content, embed=embed, ephemeral=ephemeral, view=view)
        self._interaction._last_followup = message
        self._sink.append(
            {
                "kind": "followup",
                "content": content,
                "embed": embed,
                "ephemeral": ephemeral,
                "view": type(view).__name__ if view else None,
                "message_id": message.id,
            }
        )
        if wait:
            return message
        return None


class FakeMessage:
    def __init__(
        self,
        channel: "FakeTextChannel",
        message_id: int,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
    ) -> None:
        self.channel = channel
        self.id = message_id
        self.content = content
        self.embeds = embeds if embeds is not None else ([embed] if embed else [])
        self.view = view
        self.ephemeral = ephemeral

    async def edit(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        if content is not None:
            self.content = content
        if embeds is not None:
            self.embeds = embeds
        elif embed is not None:
            self.embeds = [embed]
        self.view = view
        self.channel.edit_log.append(
            {
                "message_id": self.id,
                "content": self.content,
                "title": self.embeds[0].title if self.embeds else None,
                "view": type(view).__name__ if view else None,
            }
        )


class FakeTextChannel:
    def __init__(self, sink: list[str], channel_id: int = 444) -> None:
        self.messages = sink
        self.id = channel_id
        self._next_message_id = 1000
        self._message_store: dict[int, FakeMessage] = {}
        self.edit_log: list[dict[str, Any]] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)

    def create_message(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
    ) -> FakeMessage:
        self._next_message_id += 1
        message = FakeMessage(
            self,
            self._next_message_id,
            content=content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
        )
        self._message_store[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage | None:
        return self._message_store.get(message_id)


class FakeVoiceClient:
    def __init__(self, channel: "FakeVoiceChannel", guild: "FakeGuild") -> None:
        self.channel = channel
        self.guild = guild
        self._connected = True
        self._playing = False
        self._paused = False
        self._stop_requested = False
        self._after: Any | None = None
        self._source_task: asyncio.Task[None] | None = None
        self.source: Any | None = None
        self.bytes_streamed = 0

    def is_connected(self) -> bool:
        return self._connected

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._paused

    async def move_to(self, channel: "FakeVoiceChannel") -> None:
        self.channel = channel

    async def disconnect(self, force: bool = False) -> None:
        self._connected = False
        self._stop_requested = True
        self._playing = False
        self._paused = False
        self.guild.voice_client = None

    def pause(self) -> None:
        self._paused = True
        self._playing = False

    def resume(self) -> None:
        self._paused = False
        self._playing = True

    def stop(self) -> None:
        self._stop_requested = True
        self._playing = False
        self._paused = False

    def play(self, source: Any, after: Any) -> None:
        self.source = source
        self._after = after
        self._stop_requested = False
        self._playing = True
        self._paused = False

        async def worker() -> None:
            error: Exception | None = None
            try:
                for _ in range(240):
                    if self._stop_requested:
                        break
                    if self._paused:
                        await asyncio.sleep(0.02)
                        continue
                    chunk = await asyncio.to_thread(source.read)
                    if not chunk:
                        break
                    self.bytes_streamed += len(chunk)
                    await asyncio.sleep(0.02)
            except Exception as exc:  # pragma: no cover - runtime smoke test
                error = exc
            finally:
                self._playing = False
                self._paused = False
                self._stop_requested = False
                source.cleanup()
                callback = self._after
                self._after = None
                if callback is not None:
                    threading.Thread(target=lambda: callback(error), daemon=True).start()

        self._source_task = asyncio.create_task(worker())


class FakeVoiceChannel:
    def __init__(self, guild: "FakeGuild") -> None:
        self.guild = guild
        self.id = 9001
        self.name = "Sim Voice"

    def permissions_for(self, _member: Any) -> SimpleNamespace:
        return SimpleNamespace(connect=True, speak=True)

    async def connect(self, self_deaf: bool = True) -> FakeVoiceClient:
        voice_client = FakeVoiceClient(channel=self, guild=self.guild)
        self.guild.voice_client = voice_client
        return voice_client


class FakeGuild:
    def __init__(self, guild_id: int, bot_user_id: int) -> None:
        self.id = guild_id
        self.voice_client: FakeVoiceClient | None = None
        self.me = SimpleNamespace(id=bot_user_id)

    def get_member(self, member_id: int) -> SimpleNamespace | None:
        if member_id == self.me.id:
            return self.me
        return None


class FakeInteraction:
    def __init__(self, guild: FakeGuild, voice_channel: FakeVoiceChannel, sink: list[dict[str, Any]]) -> None:
        self.guild = guild
        self.guild_id = guild.id
        self.channel_id = 444
        self.channel = FakeTextChannel([], channel_id=self.channel_id)
        self._sink = sink
        self.user = SimpleNamespace(
            id=123,
            display_name="SimTester",
            voice=SimpleNamespace(channel=voice_channel),
        )
        self.response = FakeResponse(self, sink)
        self.followup = FakeFollowup(self, sink)
        self._original_response: FakeMessage | None = None
        self._last_followup: FakeMessage | None = None
        self.message: FakeMessage | None = None

    async def original_response(self) -> FakeMessage:
        return self._original_response

    async def edit_original_response(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        embeds: list[discord.Embed] | None = None,
        view: discord.ui.View | None = None,
    ) -> FakeMessage:
        if self._original_response is None:
            self._original_response = self.channel.create_message(
                content=content,
                embed=embed if embeds is None else None,
                view=view,
            )
            if embeds is not None:
                self._original_response.embeds = embeds
        else:
            await self._original_response.edit(content=content, embed=embed, embeds=embeds, view=view)

        self._sink.append(
            {
                "kind": "edit_original_response",
                "content": self._original_response.content,
                "embed": self._original_response.embeds[0] if self._original_response.embeds else None,
                "ephemeral": self._original_response.ephemeral,
                "view": type(self._original_response.view).__name__ if self._original_response.view else None,
                "message_id": self._original_response.id,
            }
        )
        return self._original_response


class FakeBot:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.logger = configure_logging(self.settings)
        self.loop = asyncio.get_running_loop()
        self.user = SimpleNamespace(id=999999)
        self.channel_messages: list[str] = []
        self.channels: dict[int, FakeTextChannel] = {}
        self.music = MusicManager(self, self.settings, self.logger)

    def get_channel(self, _channel_id: int) -> FakeTextChannel:
        return self.channels.get(_channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeTextChannel | None:
        return self.channels.get(channel_id)


def _track_snapshot(track: Track | None) -> dict[str, Any] | None:
    if track is None:
        return None
    return {
        "title": track.title,
        "source": track.source,
        "url": track.webpage_url,
    }


def _resolve_final_track(state) -> Track | None:
    if state.current:
        return state.current
    if state.history:
        return state.history[-1]
    if state.queue:
        return state.queue[0]
    return None


async def run_case(
    case_name: str,
    cog_class: type[MusicCog] | type[SearchCog] | type[ControlsCog],
    command_name: str,
    *command_args: Any,
    fail_register_track_message: bool = False,
    stub_single_track_playback: bool = False,
    probe_only_playback: bool = False,
    settle_seconds: float = 5.0,
) -> dict[str, Any]:
    sink: list[dict[str, Any]] = []
    bot = FakeBot()
    cog = cog_class(bot)
    guild = FakeGuild(777 + len(command_args), bot.user.id)
    voice_channel = FakeVoiceChannel(guild)
    interaction = FakeInteraction(guild, voice_channel, sink)
    interaction.channel.messages = bot.channel_messages
    bot.channels[interaction.channel_id] = interaction.channel

    if stub_single_track_playback:
        async def _stub_enqueue_query(fake_interaction: FakeInteraction, query: str, source: str = "auto") -> list[Track]:
            state = bot.music.get_state(fake_interaction.guild_id)
            state.text_channel_id = fake_interaction.channel_id
            if state.voice_client is None:
                state.voice_client = await fake_interaction.user.voice.channel.connect()

            track = Track(
                title=f"Simulated {query}",
                webpage_url="https://example.com/tracks/simulated",
                stream_url="https://example.com/tracks/simulated.mp3",
                duration=210,
                source=source,
                requester_id=fake_interaction.user.id,
                requester_name=fake_interaction.user.display_name,
                search_query=query,
            )
            state.current = track
            state.reset_progress()
            state.voice_client._playing = True
            return [track]

        bot.music.enqueue_query = _stub_enqueue_query  # type: ignore[method-assign]

    if os.getenv("AISAK_SIMULATE_YOUTUBE_FAST") == "1":
        sample_audio_path = ensure_sample_audio_file()

        def _simulated_youtube_track(query: str, requester_name: str, requester_id: int, source: str) -> Track:
            return Track(
                title=query,
                webpage_url=f"https://www.youtube.com/watch?v=sim-{abs(hash(query))}",
                stream_url=str(sample_audio_path),
                duration=180,
                uploader="YouTube",
                thumbnail="https://i.ytimg.com/vi/simulated/hqdefault.jpg",
                source="youtube",
                requester_id=requester_id,
                requester_name=requester_name,
                search_query=query,
            )

        async def _fast_fetch_tracks(
            query: str,
            requester_name: str,
            requester_id: int,
            limit: int = 1,
            source: str = "auto",
        ) -> list[Track]:
            if source in {"youtube", "auto"}:
                return [_simulated_youtube_track(query, requester_name, requester_id, source)]
            return await original_fetch_tracks(query, requester_name, requester_id, limit, source)

        async def _fast_search_tracks(
            query: str,
            requester_name: str,
            requester_id: int,
            limit: int = 5,
            source: str = "auto",
        ) -> list[Track]:
            if source in {"youtube", "auto"}:
                return [
                    _simulated_youtube_track(f"{query} result {index}", requester_name, requester_id, source)
                    for index in range(1, min(limit, 5) + 1)
                ]
            return await original_search_tracks(query, requester_name, requester_id, limit, source)

        async def _fast_prepare_stream(track: Track) -> Track:
            if track.source == "youtube":
                track.stream_url = track.stream_url or str(sample_audio_path)
                return track
            return await original_prepare_stream(track)

        original_fetch_tracks = bot.music.audio_service.fetch_tracks
        original_search_tracks = bot.music.audio_service.search_tracks
        original_prepare_stream = bot.music.audio_service.prepare_stream
        bot.music.audio_service.fetch_tracks = _fast_fetch_tracks  # type: ignore[method-assign]
        bot.music.audio_service.search_tracks = _fast_search_tracks  # type: ignore[method-assign]
        bot.music.audio_service.prepare_stream = _fast_prepare_stream  # type: ignore[method-assign]

    if probe_only_playback:
        async def _probe_start_next(
            guild_id: int,
            *,
            seek_seconds: int = 0,
            heading: str = "Reproduccion iniciada",
        ) -> Track | None:
            state = bot.music.get_state(guild_id)
            if not state.queue:
                state.current = None
                return None

            next_track = state.queue.popleft()
            prepared = await bot.music.audio_service.prepare_stream(next_track)
            state.current = prepared
            state.reset_progress()
            if state.voice_client is not None:
                state.voice_client._playing = True
                state.voice_client.bytes_streamed = max(state.voice_client.bytes_streamed, 65536)
            try:
                await bot.music.promote_track_message(guild_id, prepared, heading=heading)
            except Exception:
                pass
            return prepared

        bot.music.start_next = _probe_start_next  # type: ignore[method-assign]

    if fail_register_track_message:
        async def _failing_register_track_message(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("simulated register_track_message failure")

        bot.music.register_track_message = _failing_register_track_message  # type: ignore[method-assign]

    command = getattr(cog, command_name).callback
    error: str | None = None
    try:
        await command(cog, interaction, *command_args)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    await asyncio.sleep(settle_seconds)

    state = bot.music.get_state(guild.id)
    final_track = _resolve_final_track(state)
    voice_client = guild.voice_client

    return {
        "case": case_name,
        "command": command_name,
        "responses": [
            {
                "kind": item["kind"],
                "title": item["embed"].title if item.get("embed") else None,
                "description": item["embed"].description if item.get("embed") else item.get("content"),
                "ephemeral": item.get("ephemeral"),
                "view": item.get("view"),
            }
            for item in sink
        ],
        "bytes_streamed": getattr(voice_client, "bytes_streamed", 0),
        "channel_messages": list(bot.channel_messages),
        "message_edits": list(interaction.channel.edit_log),
        "final_track": _track_snapshot(final_track),
        "stream_url_resolved": bool(final_track and final_track.stream_url),
        "queue_length": len(state.queue),
        "history_length": len(state.history),
        "error": error,
    }


async def run_queue_response_case(case_name: str) -> dict[str, Any]:
    first_sink: list[dict[str, Any]] = []
    second_sink: list[dict[str, Any]] = []
    bot = FakeBot()
    cog = MusicCog(bot)
    guild = FakeGuild(9200, bot.user.id)
    voice_channel = FakeVoiceChannel(guild)
    channel = FakeTextChannel(bot.channel_messages, channel_id=444)
    bot.channels[channel.id] = channel
    queued_track: Track | None = None

    async def _stub_enqueue_query(fake_interaction: FakeInteraction, query: str, source: str = "auto") -> list[Track]:
        nonlocal queued_track
        state = bot.music.get_state(fake_interaction.guild_id)
        state.text_channel_id = fake_interaction.channel_id
        if state.voice_client is None:
            state.voice_client = await fake_interaction.user.voice.channel.connect()

        if state.current is None:
            current_track = Track(
                title="Simulated Primera",
                webpage_url="https://example.com/tracks/primera",
                stream_url="https://example.com/tracks/primera.mp3",
                duration=210,
                source=source,
                requester_id=fake_interaction.user.id,
                requester_name=fake_interaction.user.display_name,
                search_query=query,
            )
            state.current = current_track
            state.reset_progress()
            state.voice_client._playing = True
            return [current_track]

        queued_track = Track(
            title="Simulated Segunda",
            webpage_url="https://example.com/tracks/segunda",
            duration=180,
            source=source,
            requester_id=fake_interaction.user.id,
            requester_name=fake_interaction.user.display_name,
            search_query=query,
        )
        state.queue.append(queued_track)
        return [queued_track]

    bot.music.enqueue_query = _stub_enqueue_query  # type: ignore[method-assign]

    register_calls = 0

    async def _selective_register(*args: Any, **kwargs: Any) -> None:
        nonlocal register_calls
        register_calls += 1
        if register_calls >= 2:
            raise RuntimeError("simulated register_track_message failure on queued track")

    bot.music.register_track_message = _selective_register  # type: ignore[method-assign]

    first_interaction = FakeInteraction(guild, voice_channel, first_sink)
    first_interaction.channel = channel
    first_interaction.channel_id = channel.id
    await cog.play.callback(cog, first_interaction, "Primera", app_commands.Choice(name="auto", value="auto"))

    second_interaction = FakeInteraction(guild, voice_channel, second_sink)
    second_interaction.channel = channel
    second_interaction.channel_id = channel.id
    error: str | None = None
    try:
        await cog.play.callback(cog, second_interaction, "Segunda", app_commands.Choice(name="auto", value="auto"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    state = bot.music.get_state(guild.id)
    return {
        "case": case_name,
        "responses": [
            {
                "step": "first",
                "kind": item["kind"],
                "title": item["embed"].title if item.get("embed") else None,
                "description": item["embed"].description if item.get("embed") else item.get("content"),
                "view": item.get("view"),
            }
            for item in first_sink
        ]
        + [
            {
                "step": "second",
                "kind": item["kind"],
                "title": item["embed"].title if item.get("embed") else None,
                "description": item["embed"].description if item.get("embed") else item.get("content"),
                "view": item.get("view"),
            }
            for item in second_sink
        ],
        "current_track": _track_snapshot(state.current),
        "queued_track": _track_snapshot(state.queue[0]) if state.queue else _track_snapshot(queued_track),
        "queue_length": len(state.queue),
        "error": error,
    }


async def run_audio_controls_case(
    case_name: str,
    command_name: str,
    *command_args: Any,
    initial_speed: float = 1.0,
    initial_pitch: int = 0,
    initial_filter: AudioFilterPreset = AudioFilterPreset.OFF,
) -> dict[str, Any]:
    sink: list[dict[str, Any]] = []
    bot = FakeBot()
    cog = ControlsCog(bot)
    guild = FakeGuild(9900 + len(command_args), bot.user.id)
    voice_channel = FakeVoiceChannel(guild)
    interaction = FakeInteraction(guild, voice_channel, sink)
    interaction.channel.messages = bot.channel_messages
    bot.channels[interaction.channel_id] = interaction.channel

    sample_audio_path = ensure_sample_audio_file()
    state = bot.music.get_state(guild.id)
    state.text_channel_id = interaction.channel_id
    state.voice_client = await voice_channel.connect()
    state.playback_speed = initial_speed
    state.pitch_semitones = initial_pitch
    state.filter_preset = initial_filter

    async def _prepare_stream(track: Track) -> Track:
        if not track.stream_url:
            track.stream_url = str(sample_audio_path)
        return track

    bot.music.audio_service.prepare_stream = _prepare_stream  # type: ignore[method-assign]

    track = Track(
        title="Simulated Local Tone",
        webpage_url="https://example.com/local-tone",
        stream_url=str(sample_audio_path),
        duration=6,
        source="local",
        requester_id=interaction.user.id,
        requester_name=interaction.user.display_name,
        search_query="local-tone",
    )
    state.queue.append(track)
    await bot.music.start_next(guild.id)
    await asyncio.sleep(0.15)

    command = getattr(cog, command_name).callback
    error: str | None = None
    try:
        await command(cog, interaction, *command_args)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    await asyncio.sleep(0.75)
    state = bot.music.get_state(guild.id)
    voice_client = guild.voice_client
    current_track = _resolve_final_track(state)

    result = {
        "case": case_name,
        "command": command_name,
        "responses": [
            {
                "kind": item["kind"],
                "title": item["embed"].title if item.get("embed") else None,
                "description": item["embed"].description if item.get("embed") else item.get("content"),
                "ephemeral": item.get("ephemeral"),
                "view": item.get("view"),
            }
            for item in sink
        ],
        "bytes_streamed": getattr(voice_client, "bytes_streamed", 0),
        "current_track": _track_snapshot(current_track),
        "audio_profile": describe_audio_effects(state),
        "ffmpeg_filters": build_ffmpeg_filter_chain(state),
        "manual_restart_pending": state.manual_restart,
        "error": error,
    }
    await bot.music.disconnect(guild.id)
    return result


async def main() -> None:
    auto_choice = app_commands.Choice(name="auto", value="auto")
    nightcore_choice = app_commands.Choice(name="nightcore", value="nightcore")
    youtube_choice = app_commands.Choice(name="youtube", value="youtube")
    kimetsu_query = "Kimetsu no Yaiba Season 3 OP. Full | Kizuna no Kiseki - Sub. Español 『AMV』♡"
    playlist_url = "https://youtube.com/playlist?list=PL_ZVTvNsmBNYtGwPsmJ6xy1BvPCjSkYux&si=Yqmgi55edRY5oEm9"
    selected_cases = set(sys.argv[1:])
    results: list[dict[str, Any]] = []

    async def add_case(
        case_name: str,
        cog_class: type[MusicCog] | type[SearchCog],
        command_name: str,
        *command_args: Any,
        **case_kwargs: Any,
    ) -> None:
        if selected_cases and case_name not in selected_cases:
            return
        results.append(await run_case(case_name, cog_class, command_name, *command_args, **case_kwargs))

    await add_case(
            "search_auto_acido_iii",
            SearchCog,
            "search",
            "ACIDO III",
            auto_choice,
            settle_seconds=0.1,
        )
    await add_case(
            "play_auto_acido_iii",
            MusicCog,
            "play",
            "ACIDO III",
            auto_choice,
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "play_local_register_panel_failure_keeps_response",
            MusicCog,
            "play",
            "ACIDO III",
            auto_choice,
            stub_single_track_playback=True,
            fail_register_track_message=True,
            settle_seconds=0.05,
        )
    await add_case(
            "play_register_panel_failure_keeps_response",
            MusicCog,
            "play",
            "ACIDO III",
            auto_choice,
            fail_register_track_message=True,
        )
    await add_case(
            "youtube_play_kimetsu_kizuna_exact",
            MusicCog,
            "youtube",
            kimetsu_query,
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "playlist_youtube_ordered_queue",
            MusicCog,
            "playlist",
            playlist_url,
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "youtube_play_blinding_lights",
            MusicCog,
            "youtube",
            "The Weeknd Blinding Lights",
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "youtube_play_numb",
            MusicCog,
            "youtube",
            "Linkin Park Numb",
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "youtube_play_one_more_time",
            MusicCog,
            "youtube",
            "Daft Punk One More Time",
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "youtube_play_let_down",
            MusicCog,
            "youtube",
            "Radiohead Let Down",
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "youtube_play_humble",
            MusicCog,
            "youtube",
            "Kendrick Lamar HUMBLE",
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    await add_case(
            "play_explicit_youtube_smoke",
            MusicCog,
            "play",
            "Linkin Park Numb",
            youtube_choice,
            probe_only_playback=True,
            settle_seconds=0.1,
        )
    if not selected_cases or "play_second_track_queue_response_keeps_message" in selected_cases:
        results.append(await run_queue_response_case("play_second_track_queue_response_keeps_message"))
    if not selected_cases or "controls_speed_live_refresh" in selected_cases:
        results.append(
            await run_audio_controls_case(
                "controls_speed_live_refresh",
                "speed",
                1.15,
            )
        )
    if not selected_cases or "controls_filter_live_refresh" in selected_cases:
        results.append(
            await run_audio_controls_case(
                "controls_filter_live_refresh",
                "filter",
                nightcore_choice,
            )
        )
    if not selected_cases or "controls_effectsreset_restores_normal" in selected_cases:
        results.append(
            await run_audio_controls_case(
                "controls_effectsreset_restores_normal",
                "effectsreset",
                initial_speed=1.1,
                initial_pitch=3,
                initial_filter=AudioFilterPreset.BASSBOOST,
            )
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
