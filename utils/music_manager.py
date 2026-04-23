from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import discord

from config import Settings
from utils.audio_handler import AudioService
from utils.errors import PermissionError, PlaybackError, UserInputError
from utils.formatters import build_now_playing_embed, build_queue_embed, build_success_embed, build_track_embed
from utils.models import GuildMusicState, MessageRef, RepeatMode, Track
from utils.player_controls import PlayerControlsView
from utils.validators import is_url

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTIONS = "-vn -sn -dn -loglevel warning"


class MusicManager:
    def __init__(self, bot: discord.Client, settings: Settings, logger: logging.Logger) -> None:
        self.bot = bot
        self.settings = settings
        self.logger = logger.getChild("music")
        self.audio_service = AudioService(settings, logger)
        self.states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(
                guild_id=guild_id,
                volume=self.settings.default_volume_ratio,
            )
        return self.states[guild_id]

    async def ensure_voice(self, interaction: discord.Interaction) -> GuildMusicState:
        guild = interaction.guild
        if guild is None:
            raise UserInputError("Este comando solo funciona dentro de un servidor.")

        voice_state = getattr(interaction.user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            raise UserInputError("Debes estar en un canal de voz para usar este comando.")

        channel = voice_state.channel
        me = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        if me is None:
            raise PermissionError("No pude verificar los permisos del bot en este servidor.")

        permissions = channel.permissions_for(me)
        if not permissions.connect or not permissions.speak:
            raise PermissionError("Necesito permisos de `Connect` y `Speak` en ese canal de voz.")

        state = self.get_state(guild.id)
        state.text_channel_id = interaction.channel_id

        async with state.connect_lock:
            voice_client = guild.voice_client

            if voice_client and not voice_client.is_connected():
                self.logger.warning(
                    "Voice client desconectado detectado en guild=%s; recreando conexion.",
                    guild.id,
                )
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    self.logger.exception("No pude limpiar el voice client previo en guild=%s", guild.id)
                voice_client = None

            if voice_client and voice_client.channel != channel:
                self.logger.info(
                    "Moviendo voice client en guild=%s de canal=%s a canal=%s",
                    guild.id,
                    getattr(voice_client.channel, "id", None),
                    channel.id,
                )
                await voice_client.move_to(channel)
            elif voice_client is None:
                self.logger.info("Creando nueva conexion de voz en guild=%s canal=%s", guild.id, channel.id)
                voice_client = await channel.connect(self_deaf=True)

            state.voice_client = voice_client
        await self._cancel_auto_disconnect(state)
        return state

    async def enqueue_query(self, interaction: discord.Interaction, query: str, source: str = "auto") -> list[Track]:
        state = await self.ensure_voice(interaction)
        expand_query = self.audio_service.should_expand_query(query)
        available_slots = self._available_slots(state)
        requested_limit = available_slots if expand_query else 1
        tracks = await self.audio_service.fetch_tracks(
            query=query,
            requester_name=interaction.user.display_name,
            requester_id=interaction.user.id,
            limit=requested_limit,
            source=source,
        )

        if (
            not expand_query
            and not is_url(query)
            and not state.queue
            and not self.is_playing(state.guild_id)
        ):
            tracks = [await self._select_first_playable_track(tracks)]

        return await self.enqueue_tracks(interaction, tracks)

    async def enqueue_tracks(self, interaction: discord.Interaction, tracks: Iterable[Track]) -> list[Track]:
        state = await self.ensure_voice(interaction)
        tracks = list(tracks)
        if not tracks:
            raise PlaybackError("No hay canciones para agregar.")

        available_slots = self._available_slots(state)
        accepted = tracks[:available_slots]
        if not accepted:
            raise UserInputError("La cola alcanzo su limite maximo.")

        for track in accepted:
            state.queue.append(track)

        if not self.is_playing(state.guild_id):
            await self.start_next(state.guild_id)

        return accepted

    async def search(self, interaction: discord.Interaction, query: str, limit: int = 5, source: str = "auto") -> list[Track]:
        await self.ensure_voice(interaction)
        return await self.audio_service.search_tracks(
            query=query,
            requester_name=interaction.user.display_name,
            requester_id=interaction.user.id,
            limit=limit,
            source=source,
        )

    async def start_next(self, guild_id: int) -> Track | None:
        state = self.get_state(guild_id)

        async with state.play_lock:
            voice_client = state.voice_client
            if voice_client is None or not voice_client.is_connected():
                self.logger.warning(
                    "No hay conexion de voz activa para iniciar reproduccion en guild=%s",
                    guild_id,
                )
                state.current = None
                return None

            await self._cancel_auto_disconnect(state)

            while state.queue:
                next_track = state.queue.popleft()
                try:
                    prepared_track = await self.audio_service.prepare_stream(next_track)
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(
                            prepared_track.stream_url,
                            before_options=self._build_ffmpeg_before_options(prepared_track),
                            options=FFMPEG_OPTIONS,
                        ),
                        volume=state.volume,
                    )
                    state.current = prepared_track
                    state.reset_progress()
                    voice_client.play(
                        source,
                        after=lambda error, gid=guild_id: self._after_playback(gid, error),
                    )
                    await self.promote_track_message(guild_id, prepared_track, heading="Reproduccion iniciada")
                    self.logger.info("Reproduciendo en guild=%s: %s", guild_id, prepared_track.title)
                    return prepared_track
                except Exception as exc:  # pragma: no cover - runtime/network path
                    self.logger.exception("Fallo preparando el audio para %s", next_track.title)
                    await self._notify_text_channel(
                        state,
                        self._format_playback_failure_message(next_track, exc),
                    )
                    continue

            state.current = None
            state.clear_progress()
            await self.disable_active_panel(guild_id)
            await self._schedule_auto_disconnect(state)
            return None

    def _after_playback(self, guild_id: int, error: Exception | None) -> None:
        future = asyncio.run_coroutine_threadsafe(self._handle_after_playback(guild_id, error), self.bot.loop)
        try:
            future.result()
        except Exception:  # pragma: no cover - runtime path
            self.logger.exception("Error manejando fin de reproduccion en guild=%s", guild_id)

    async def _handle_after_playback(self, guild_id: int, error: Exception | None) -> None:
        state = self.get_state(guild_id)
        current = state.current

        if error:
            self.logger.error("FFmpeg/Voice error en guild=%s: %s", guild_id, error)
            await self._notify_text_channel(state, "Hubo un error reproduciendo la cancion actual. Intentare continuar.")

        if state.manual_stop:
            state.manual_stop = False
            state.manual_skip = False
            state.current = None
            state.clear_progress()
            await self.disconnect(guild_id)
            return

        if current is not None:
            if state.repeat_mode == RepeatMode.ONE and not state.manual_skip:
                state.queue.appendleft(current)
            else:
                state.history.append(current)
                if state.repeat_mode == RepeatMode.ALL and not state.manual_skip:
                    state.queue.append(current)

        state.manual_skip = False
        state.current = None
        state.clear_progress()
        if not state.queue and state.autoplay_enabled and current is not None:
            await self._enqueue_autoplay_track(state, current)
        await self.start_next(guild_id)

    async def pause(self, guild_id: int) -> Track:
        state = self.get_state(guild_id)
        if not state.voice_client or not state.voice_client.is_playing() or state.current is None:
            raise PlaybackError("No hay una cancion reproduciendose ahora mismo.")
        state.voice_client.pause()
        state.paused_at = discord.utils.utcnow()
        return state.current

    async def resume(self, guild_id: int) -> Track:
        state = self.get_state(guild_id)
        if not state.voice_client or not state.voice_client.is_paused() or state.current is None:
            raise PlaybackError("No hay ninguna cancion pausada.")
        if state.paused_at is not None:
            state.paused_seconds += (discord.utils.utcnow() - state.paused_at).total_seconds()
            state.paused_at = None
        state.voice_client.resume()
        return state.current

    async def skip(self, guild_id: int, count: int = 1) -> Track | None:
        state = self.get_state(guild_id)
        if count > 1:
            for _ in range(max(0, count - 1)):
                if state.queue:
                    state.queue.popleft()

        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.manual_skip = True
            current = state.current
            state.voice_client.stop()
            return current

        if state.queue:
            return await self.start_next(guild_id)

        raise PlaybackError("No hay canciones para saltar.")

    async def stop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        state.queue.clear()
        state.repeat_mode = RepeatMode.OFF
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.manual_stop = True
            state.voice_client.stop()
            return
        await self.disable_active_panel(guild_id)
        await self.disconnect(guild_id)

    async def disconnect(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        await self._cancel_auto_disconnect(state)
        await self.disable_active_panel(guild_id)
        if state.voice_client and state.voice_client.is_connected():
            await state.voice_client.disconnect(force=False)
        state.voice_client = None
        state.current = None
        state.queue.clear()
        state.clear_progress()
        state.manual_skip = False
        state.manual_stop = False
        state.track_messages.clear()

    def remove_from_queue(self, guild_id: int, position: int) -> Track:
        state = self.get_state(guild_id)
        queue_items = list(state.queue)
        if position < 1 or position > len(queue_items):
            raise UserInputError("La posicion indicada no existe en la cola.")
        removed = queue_items.pop(position - 1)
        state.queue.clear()
        state.queue.extend(queue_items)
        return removed

    def clear_queue(self, guild_id: int) -> int:
        state = self.get_state(guild_id)
        cleared = len(state.queue)
        state.queue.clear()
        return cleared

    def shuffle_queue(self, guild_id: int) -> int:
        import random

        state = self.get_state(guild_id)
        queue_items = list(state.queue)
        random.shuffle(queue_items)
        state.queue.clear()
        state.queue.extend(queue_items)
        return len(queue_items)

    def set_repeat_mode(self, guild_id: int, mode: RepeatMode) -> RepeatMode:
        state = self.get_state(guild_id)
        state.repeat_mode = mode
        return state.repeat_mode

    def set_volume(self, guild_id: int, volume: int) -> int:
        state = self.get_state(guild_id)
        state.volume = volume / 100
        if state.voice_client and state.voice_client.source and hasattr(state.voice_client.source, "volume"):
            state.voice_client.source.volume = state.volume
        return volume

    def toggle_autoplay(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        state.autoplay_enabled = not state.autoplay_enabled
        return state.autoplay_enabled

    def is_playing(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        return bool(
            state.voice_client
            and state.voice_client.is_connected()
            and (state.voice_client.is_playing() or state.voice_client.is_paused())
        )

    async def _schedule_auto_disconnect(self, state: GuildMusicState) -> None:
        if state.auto_disconnect_task and not state.auto_disconnect_task.done():
            return

        async def worker() -> None:
            await asyncio.sleep(self.settings.inactivity_timeout)
            voice_client = state.voice_client
            if not voice_client or not voice_client.is_connected():
                return
            if voice_client.is_playing() or voice_client.is_paused():
                return
            await self._notify_text_channel(
                state,
                "No hubo actividad por varios minutos, asi que me desconecte del canal de voz.",
            )
            await self.disconnect(state.guild_id)

        state.auto_disconnect_task = asyncio.create_task(worker(), name=f"aisak-idle-{state.guild_id}")

    async def _cancel_auto_disconnect(self, state: GuildMusicState) -> None:
        task = state.auto_disconnect_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state.auto_disconnect_task = None

    async def _notify_text_channel(self, state: GuildMusicState, message: str) -> None:
        if not state.text_channel_id:
            return
        channel = self.bot.get_channel(state.text_channel_id)
        if channel and hasattr(channel, "send"):
            try:
                await channel.send(message)
            except discord.HTTPException:
                self.logger.debug("No se pudo enviar mensaje informativo al canal %s", state.text_channel_id)

    def _available_slots(self, state: GuildMusicState) -> int:
        current_count = len(state.queue) + (1 if state.current else 0)
        return max(0, self.settings.max_queue_length - current_count)

    def _build_ffmpeg_before_options(self, track: Track) -> str:
        options = [
            FFMPEG_BEFORE_OPTIONS,
            "-protocol_whitelist file,http,https,tcp,tls,crypto",
            "-rw_timeout 15000000",
        ]

        if track.stream_headers:
            serialized_headers = "".join(f"{key}: {value}\r\n" for key, value in track.stream_headers.items())
            escaped_headers = serialized_headers.replace('"', '\\"')
            options.append(f'-headers "{escaped_headers}"')

            user_agent = track.stream_headers.get("User-Agent")
            if user_agent:
                escaped_user_agent = user_agent.replace('"', '\\"')
                options.append(f'-user_agent "{escaped_user_agent}"')

        return " ".join(options)

    def _format_playback_failure_message(self, track: Track, exc: Exception) -> str:
        if isinstance(exc, PlaybackError):
            return f"No pude reproducir **{track.title}**: {exc} Pase al siguiente tema."
        return f"No pude reproducir **{track.title}** por un fallo interno. Pase al siguiente tema."

    async def _enqueue_autoplay_track(self, state: GuildMusicState, seed_track: Track) -> None:
        requester_id = self.bot.user.id if self.bot.user else 0
        seed_query = self._build_autoplay_query(seed_track)
        try:
            candidates = await self.audio_service.search_tracks(
                query=seed_query,
                requester_name="AutoPlay",
                requester_id=requester_id,
                limit=5,
                source="auto",
            )
        except Exception:
            self.logger.exception("AutoPlay fallo buscando relacionados para %s", seed_track.title)
            return

        seen_urls = {seed_track.webpage_url}
        seen_urls.update(track.webpage_url for track in state.queue)
        seen_urls.update(track.webpage_url for track in state.history)

        for candidate in candidates:
            if candidate.webpage_url in seen_urls:
                continue
            candidate.requester_name = "AutoPlay"
            candidate.requester_id = requester_id
            state.queue.append(candidate)
            await self._notify_text_channel(state, f"AutoPlay agrego **{candidate.title}** a la cola.")
            return

    def _build_autoplay_query(self, track: Track) -> str:
        if track.search_query:
            return track.search_query
        if track.uploader and track.uploader.lower() not in track.title.lower():
            return f"{track.uploader} {track.title}"
        return track.title

    async def _select_first_playable_track(self, candidates: list[Track]) -> Track:
        if not candidates:
            raise PlaybackError("No encontre canciones para reproducir.")

        last_error: PlaybackError | None = None
        for candidate in candidates:
            try:
                return await self.audio_service.prepare_stream(candidate)
            except PlaybackError as exc:
                last_error = exc
                self.logger.info("Descartando candidato no reproducible: %s (%s)", candidate.title, exc)
                continue

        if last_error is not None:
            raise last_error
        raise PlaybackError("No encontre una pista reproducible entre los resultados.")

    async def register_track_message(
        self,
        guild_id: int,
        track: Track,
        *,
        channel_id: int,
        message_id: int,
        activate: bool,
        heading: str,
    ) -> None:
        state = self.get_state(guild_id)
        ref = MessageRef(channel_id=channel_id, message_id=message_id)
        state.track_messages[track.webpage_url] = ref
        if activate:
            await self.promote_track_message(guild_id, track, heading=heading)

    async def promote_track_message(self, guild_id: int, track: Track, *, heading: str) -> None:
        state = self.get_state(guild_id)
        target_ref = state.track_messages.get(track.webpage_url) or state.active_panel
        if target_ref is None:
            return

        if state.active_panel and (
            state.active_panel.channel_id != target_ref.channel_id or state.active_panel.message_id != target_ref.message_id
        ):
            await self._clear_message_controls(state.active_panel)

        message = await self._fetch_message(target_ref)
        if message is None:
            return

        await message.edit(
            embed=build_track_embed(
                track,
                heading,
                self.settings.bot_color,
                voice_channel_name=self._voice_channel_name(state),
            ),
            view=PlayerControlsView(self.bot, guild_id),
        )
        state.active_panel = target_ref
        state.active_panel_track_url = track.webpage_url

    async def refresh_active_panel(
        self,
        guild_id: int,
        *,
        heading: str | None = None,
        dashboard: bool = False,
    ) -> None:
        state = self.get_state(guild_id)
        if state.active_panel is None:
            return

        message = await self._fetch_message(state.active_panel)
        if message is None:
            state.active_panel = None
            state.active_panel_track_url = None
            return

        if state.current is None:
            await message.edit(
                embed=build_success_embed(
                    "Reproduccion detenida",
                    "No queda ninguna cancion activa en reproduccion.",
                    self.settings.bot_color,
                ),
                view=None,
            )
            state.active_panel = None
            state.active_panel_track_url = None
            return

        if dashboard:
            await message.edit(
                embeds=[
                    build_now_playing_embed(state, self.settings.bot_color),
                    build_queue_embed(state, 1, self.settings.bot_color),
                ],
                content=None,
                view=PlayerControlsView(self.bot, guild_id),
            )
            return

        await message.edit(
            embed=build_track_embed(
                state.current,
                heading or "Reproduccion iniciada",
                self.settings.bot_color,
                voice_channel_name=self._voice_channel_name(state),
            ),
            view=PlayerControlsView(self.bot, guild_id),
        )
        state.active_panel_track_url = state.current.webpage_url

    async def disable_active_panel(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.active_panel is not None:
            await self._clear_message_controls(
                state.active_panel,
                finished_track=state.history[-1] if state.history else None,
            )
        if state.active_panel_track_url:
            state.track_messages.pop(state.active_panel_track_url, None)
        state.active_panel = None
        state.active_panel_track_url = None

    async def _clear_message_controls(self, ref: MessageRef, finished_track: Track | None = None) -> None:
        message = await self._fetch_message(ref)
        if message is None:
            return
        try:
            if finished_track is not None:
                await message.edit(
                    embed=build_track_embed(
                        finished_track,
                        "Reproduccion finalizada",
                        self.settings.bot_color,
                        voice_channel_name=None,
                    ),
                    view=None,
                )
            else:
                await message.edit(view=None)
        except discord.HTTPException:
            self.logger.debug("No pude limpiar controles del mensaje %s", ref.message_id)

    async def _fetch_message(self, ref: MessageRef):
        channel = self.bot.get_channel(ref.channel_id)
        if channel is None and hasattr(self.bot, "fetch_channel"):
            try:
                channel = await self.bot.fetch_channel(ref.channel_id)
            except Exception:
                return None
        if channel is None or not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(ref.message_id)
        except Exception:
            return None

    def _voice_channel_name(self, state: GuildMusicState) -> str | None:
        channel = getattr(state.voice_client, "channel", None)
        return getattr(channel, "name", None)
