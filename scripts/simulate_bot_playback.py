from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import get_settings
from cogs.music import MusicCog
from cogs.search import SearchCog
from utils.logger import configure_logging
from utils.models import Track
from utils.music_manager import MusicManager


class FakeResponse:
    def __init__(self, interaction: "FakeInteraction", sink: list[dict[str, Any]]) -> None:
        self._interaction = interaction
        self._done = False
        self._sink = sink

    async def defer(self, thinking: bool = False) -> None:
        self._done = True
        self._sink.append({"kind": "defer", "thinking": thinking})

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
        self.guild.voice_client = None

    def pause(self) -> None:
        self._paused = True
        self._playing = False

    def resume(self) -> None:
        self._paused = False
        self._playing = True

    def stop(self) -> None:
        self._playing = False
        self._paused = False

    def play(self, source: Any, after: Any) -> None:
        self.source = source
        self._playing = True
        self._paused = False

        async def worker() -> None:
            error: Exception | None = None
            try:
                for _ in range(60):
                    chunk = await asyncio.to_thread(source.read)
                    if not chunk:
                        break
                    self.bytes_streamed += len(chunk)
            except Exception as exc:  # pragma: no cover - runtime smoke test
                error = exc
            finally:
                self._playing = False
                self._paused = False
                source.cleanup()
                threading.Thread(target=lambda: after(error), daemon=True).start()

        asyncio.create_task(worker())


class FakeVoiceChannel:
    def __init__(self, guild: "FakeGuild") -> None:
        self.guild = guild
        self.id = 9001

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
    cog_class: type[MusicCog] | type[SearchCog],
    command_name: str,
    *command_args: Any,
) -> dict[str, Any]:
    sink: list[dict[str, Any]] = []
    bot = FakeBot()
    cog = cog_class(bot)
    guild = FakeGuild(777 + len(command_args), bot.user.id)
    voice_channel = FakeVoiceChannel(guild)
    interaction = FakeInteraction(guild, voice_channel, sink)
    interaction.channel.messages = bot.channel_messages
    bot.channels[interaction.channel_id] = interaction.channel

    command = getattr(cog, command_name).callback
    await command(cog, interaction, *command_args)
    await asyncio.sleep(5)

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
        "queue_length": len(state.queue),
        "history_length": len(state.history),
    }


async def main() -> None:
    soundcloud_choice = app_commands.Choice(name="auto", value="auto")
    playlist_url = "https://youtube.com/playlist?list=PL_ZVTvNsmBNYtGwPsmJ6xy1BvPCjSkYux&si=Yqmgi55edRY5oEm9"

    results = [
        await run_case(
            "search_auto_acido_iii",
            SearchCog,
            "search",
            "ACIDO III",
            soundcloud_choice,
        ),
        await run_case(
            "play_auto_acido_iii",
            MusicCog,
            "play",
            "ACIDO III",
            soundcloud_choice,
        ),
        await run_case(
            "play_soundcloud_url_acido_iii",
            MusicCog,
            "play",
            "https://soundcloud.com/goodcookie-002/acido-iii-1",
            app_commands.Choice(name="soundcloud", value="soundcloud"),
        ),
        await run_case(
            "play_mixcloud_direct_url",
            MusicCog,
            "mixcloud",
            "https://www.mixcloud.com/dholbach/cryptkeeper/",
        ),
        await run_case(
            "playlist_youtube_ordered_queue",
            MusicCog,
            "playlist",
            playlist_url,
        ),
    ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
