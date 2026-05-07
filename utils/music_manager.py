from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path
from typing import Iterable

import discord

from config import Settings
from utils.audio_effects import build_ffmpeg_filter_chain
from utils.audio_handler import AudioService
from utils.errors import PermissionError, PlaybackError, UserInputError
from utils.formatters import build_now_playing_embed, build_queue_embed, build_success_embed, build_track_embed
from utils.models import AudioFilterPreset, GuildMusicState, MessageRef, RepeatMode, Track
from utils.player_controls import PlayerControlsView
from utils.validators import is_url

FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_at_eof 1 "
    "-reconnect_on_network_error 1 "
    '-reconnect_on_http_error 4xx,5xx '
    "-reconnect_max_retries 8 "
    "-reconnect_delay_total_max 30 "
    "-respect_retry_after 1 "
    "-thread_queue_size 1024 "
    "-reconnect_delay_max 10 "
    "-nostdin"
)
FFMPEG_OPTIONS_BASE = "-vn -sn -dn -bufsize 192K -loglevel warning"


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
                raise PermissionError(
                    "Ya estoy conectado en otro canal de voz. Entra al mismo canal que el bot para seguir controlando la sesion."
                )
            elif voice_client is None:
                self.logger.info("Creando nueva conexion de voz en guild=%s canal=%s", guild.id, channel.id)
                voice_client = await channel.connect(self_deaf=True)

            state.voice_client = voice_client
        await self._cancel_auto_disconnect(state)
        return state

    def assert_control_access(self, interaction: discord.Interaction) -> GuildMusicState:
        guild = interaction.guild
        if guild is None:
            raise UserInputError("Este comando solo funciona dentro de un servidor.")

        state = self.get_state(guild.id)
        voice_client = state.voice_client or guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            raise UserInputError("No hay una sesion de voz activa en este servidor.")

        user_voice = getattr(interaction.user, "voice", None)
        user_channel = getattr(user_voice, "channel", None)
        if user_channel is None:
            raise UserInputError("Debes estar en el mismo canal de voz que el bot para usar este control.")

        bot_channel = getattr(voice_client, "channel", None)
        if bot_channel is not None and getattr(bot_channel, "id", None) != getattr(user_channel, "id", None):
            channel_name = getattr(bot_channel, "name", "el canal activo")
            raise PermissionError(f"Debes estar conectado en **{channel_name}** para controlar la reproduccion.")

        state.voice_client = voice_client
        state.text_channel_id = interaction.channel_id
        return state

    async def enqueue_query(self, interaction: discord.Interaction, query: str, source: str = "auto") -> list[Track]:
        state = await self.ensure_voice(interaction)
        expand_query = self.audio_service.should_expand_query(query)
        available_slots = self._available_slots(state)
        select_playable_candidate = (
            not expand_query
            and not is_url(query)
            and not state.queue
            and not self.is_playing(state.guild_id)
        )
        if expand_query:
            requested_limit = available_slots
        elif select_playable_candidate:
            requested_limit = min(available_slots, max(1, self.settings.play_candidate_limit))
        else:
            requested_limit = 1
        tracks = await self.audio_service.fetch_tracks(
            query=query,
            requester_name=interaction.user.display_name,
            requester_id=interaction.user.id,
            limit=requested_limit,
            source=source,
        )

        if select_playable_candidate:
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

    async def start_next(
        self,
        guild_id: int,
        *,
        seek_seconds: int = 0,
        heading: str = "Reproduccion iniciada",
    ) -> Track | None:
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
            resume_offset = max(0, seek_seconds)
            panel_heading = heading

            while state.queue:
                next_track = state.queue.popleft()
                try:
                    prepared_track = await self.audio_service.prepare_stream(next_track)
                    if not prepared_track.stream_url:
                        raise PlaybackError("No pude obtener el audio de la pista.")
                    ffmpeg_input = self._resolve_ffmpeg_input(prepared_track)
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(
                            ffmpeg_input,
                            before_options=self._build_ffmpeg_before_options(prepared_track, seek_seconds=resume_offset),
                            options=self._build_ffmpeg_options(state),
                        ),
                        volume=state.volume,
                    )
                    state.current = prepared_track
                    state.reset_progress()
                    if resume_offset > 0 and state.started_at is not None:
                        state.started_at -= timedelta(seconds=resume_offset)
                    voice_client.play(
                        source,
                        after=lambda error, gid=guild_id: self._after_playback(gid, error),
                    )
                    try:
                        await self.promote_track_message(guild_id, prepared_track, heading=panel_heading)
                    except Exception:
                        self.logger.exception(
                            "La reproduccion ya habia arrancado, pero el panel no pudo actualizarse en guild=%s",
                            guild_id,
                        )
                    self.logger.info("Reproduciendo en guild=%s: %s", guild_id, prepared_track.title)
                    return prepared_track
                except Exception as exc:  # pragma: no cover - runtime/network path
                    self.logger.exception("Fallo preparando el audio para %s", next_track.title)
                    recovered_track = await self._recover_blocked_youtube_candidate(state, next_track, exc)
                    if recovered_track is not None:
                        state.queue.appendleft(recovered_track)
                        resume_offset = 0
                        panel_heading = "Reproduccion iniciada"
                        continue
                    await self._notify_text_channel(
                        state,
                        self._format_playback_failure_message(next_track, exc),
                    )
                    resume_offset = 0
                    panel_heading = "Reproduccion iniciada"
                    continue

            state.current = None
            state.clear_progress()
            await self.disable_active_panel(guild_id)
            await self._schedule_auto_disconnect(state)
            return None

    def _after_playback(self, guild_id: int, error: Exception | None) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(self._handle_after_playback(guild_id, error), self.bot.loop)
        except RuntimeError:  # pragma: no cover - runtime path
            self.logger.exception("No pude programar el fin de reproduccion en guild=%s", guild_id)
            return
        future.add_done_callback(lambda done, gid=guild_id: self._log_after_playback_result(gid, done))

    def _log_after_playback_result(self, guild_id: int, future: Future[None]) -> None:
        try:
            future.result()
        except Exception:  # pragma: no cover - runtime path
            self.logger.exception("Error manejando fin de reproduccion en guild=%s", guild_id)

    async def _handle_after_playback(self, guild_id: int, error: Exception | None) -> None:
        state = self.get_state(guild_id)
        current = state.current
        restart_waiter = state.restart_waiter
        transition_waiter = state.transition_waiter

        if error:
            self.logger.error("FFmpeg/Voice error en guild=%s: %s", guild_id, error)
            await self._notify_text_channel(state, "Hubo un error reproduciendo la cancion actual. Intentare continuar.")

        if state.manual_stop:
            state.manual_stop = False
            state.manual_skip = False
            state.current = None
            state.clear_progress()
            await self.disconnect(guild_id)
            self._resolve_waiter(transition_waiter, state, "transition_waiter")
            return

        if state.manual_restart and current is not None:
            restart_position = state.restart_position_seconds
            restart_paused = state.restart_paused
            state.manual_restart = False
            state.manual_skip = False
            state.restart_position_seconds = 0
            state.restart_paused = False
            state.current = None
            state.clear_progress()
            self._invalidate_track_stream_cache(current)
            state.queue.appendleft(current)
            try:
                restarted_track = await self.start_next(
                    guild_id,
                    seek_seconds=restart_position,
                    heading="Audio actualizado",
                )
                if (
                    restart_paused
                    and restarted_track is not None
                    and state.voice_client
                    and state.voice_client.is_playing()
                ):
                    state.voice_client.pause()
                    state.paused_at = discord.utils.utcnow()
            finally:
                if restart_waiter and not restart_waiter.done():
                    restart_waiter.set_result(None)
                if state.restart_waiter is restart_waiter:
                    state.restart_waiter = None
                self._resolve_waiter(transition_waiter, state, "transition_waiter")
            return

        if current is not None:
            if state.repeat_mode == RepeatMode.ONE and not state.manual_skip:
                self._invalidate_track_stream_cache(current)
                state.queue.appendleft(current)
            else:
                state.history.append(current)
                if state.repeat_mode == RepeatMode.ALL and not state.manual_skip:
                    self._invalidate_track_stream_cache(current)
                    state.queue.append(current)

        state.manual_skip = False
        state.current = None
        state.clear_progress()
        if not state.queue and state.autoplay_enabled and current is not None:
            await self._enqueue_autoplay_track(state, current)
        await self.start_next(guild_id)
        if restart_waiter and not restart_waiter.done():
            restart_waiter.set_result(None)
        self._resolve_waiter(transition_waiter, state, "transition_waiter")

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
            if state.transition_waiter and not state.transition_waiter.done():
                state.transition_waiter.cancel()
            state.transition_waiter = self.bot.loop.create_future()
            state.voice_client.stop()
            return current

        if state.queue:
            return await self.start_next(guild_id)

        raise PlaybackError("No hay canciones para saltar.")

    async def stop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        state.queue.clear()
        state.repeat_mode = RepeatMode.OFF
        state.manual_restart = False
        state.restart_position_seconds = 0
        state.restart_paused = False
        if state.restart_waiter and not state.restart_waiter.done():
            state.restart_waiter.cancel()
        state.restart_waiter = None
        if state.transition_waiter and not state.transition_waiter.done():
            state.transition_waiter.cancel()
        state.transition_waiter = None
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
        state.manual_restart = False
        state.restart_position_seconds = 0
        state.restart_paused = False
        if state.restart_waiter and not state.restart_waiter.done():
            state.restart_waiter.cancel()
        state.restart_waiter = None
        if state.transition_waiter and not state.transition_waiter.done():
            state.transition_waiter.cancel()
        state.transition_waiter = None
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

    async def set_speed(self, guild_id: int, speed: float) -> tuple[float, bool]:
        state = self.get_state(guild_id)
        state.playback_speed = speed
        restarted = await self._refresh_current_track(guild_id)
        return state.playback_speed, restarted

    async def set_pitch(self, guild_id: int, semitones: int) -> tuple[int, bool]:
        state = self.get_state(guild_id)
        state.pitch_semitones = semitones
        restarted = await self._refresh_current_track(guild_id)
        return state.pitch_semitones, restarted

    async def set_filter(self, guild_id: int, preset: AudioFilterPreset) -> tuple[AudioFilterPreset, bool]:
        state = self.get_state(guild_id)
        state.filter_preset = preset
        restarted = await self._refresh_current_track(guild_id)
        return state.filter_preset, restarted

    async def reset_audio_effects(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        state.playback_speed = 1.0
        state.pitch_semitones = 0
        state.filter_preset = AudioFilterPreset.OFF
        return await self._refresh_current_track(guild_id)

    def toggle_autoplay(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        state.autoplay_enabled = not state.autoplay_enabled
        return state.autoplay_enabled

    def is_playing(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        voice_client = state.voice_client
        if not voice_client:
            return False
        is_connected = getattr(voice_client, "is_connected", None)
        is_playing = getattr(voice_client, "is_playing", None)
        is_paused = getattr(voice_client, "is_paused", None)
        return bool(
            callable(is_connected)
            and is_connected()
            and (
                (callable(is_playing) and is_playing())
                or (callable(is_paused) and is_paused())
            )
        )

    async def _refresh_current_track(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        voice_client = state.voice_client
        current = state.current
        if (
            current is None
            or voice_client is None
            or not voice_client.is_connected()
            or not (voice_client.is_playing() or voice_client.is_paused())
        ):
            return False

        if state.restart_waiter and not state.restart_waiter.done():
            state.restart_waiter.cancel()

        restart_position = state.elapsed_seconds()
        if current.duration:
            restart_position = min(restart_position, max(0, current.duration - 1))

        state.restart_position_seconds = restart_position
        state.restart_paused = voice_client.is_paused()
        state.manual_restart = True
        self._invalidate_track_stream_cache(current)

        waiter = self.bot.loop.create_future()
        state.restart_waiter = waiter
        voice_client.stop()

        try:
            await asyncio.wait_for(waiter, timeout=8)
        except asyncio.TimeoutError:
            self.logger.warning(
                "La actualizacion de audio no termino a tiempo en guild=%s track=%s",
                guild_id,
                current.title,
            )
        finally:
            if state.restart_waiter is waiter:
                state.restart_waiter = None
        return True

    async def wait_for_transition(self, guild_id: int, timeout: float = 6.0) -> None:
        state = self.get_state(guild_id)
        waiter = state.transition_waiter
        if waiter is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.TimeoutError:
            self.logger.warning("La transicion de reproduccion no termino a tiempo en guild=%s", guild_id)
        finally:
            if state.transition_waiter is waiter:
                state.transition_waiter = None

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

    def _build_ffmpeg_before_options(self, track: Track, *, seek_seconds: int = 0) -> str:
        if self._is_local_stream(track.stream_url):
            options = ["-nostdin"]
        else:
            options = [
                FFMPEG_BEFORE_OPTIONS,
                "-protocol_whitelist file,http,https,tcp,tls,crypto,async,cache",
                "-rw_timeout 8000000",
                "-multiple_requests 1",
            ]
            proxy_url = self._ffmpeg_proxy_url()
            if proxy_url:
                escaped_proxy = proxy_url.replace('"', '\\"')
                options.append(f'-http_proxy "{escaped_proxy}"')

        if seek_seconds > 0:
            options.append(f"-ss {seek_seconds}")

        if track.stream_headers:
            serialized_headers = "".join(f"{key}: {value}\r\n" for key, value in track.stream_headers.items())
            escaped_headers = serialized_headers.replace('"', '\\"')
            options.append(f'-headers "{escaped_headers}"')

            user_agent = track.stream_headers.get("User-Agent")
            if user_agent:
                escaped_user_agent = user_agent.replace('"', '\\"')
                options.append(f'-user_agent "{escaped_user_agent}"')

        return " ".join(options)

    def _resolve_ffmpeg_input(self, track: Track) -> str:
        stream_url = track.stream_url or ""
        if self._is_local_stream(stream_url):
            return stream_url
        if stream_url.startswith(("http://", "https://")):
            return f"async:cache:{stream_url}"
        return stream_url

    def _build_ffmpeg_options(self, state: GuildMusicState) -> str:
        options = [FFMPEG_OPTIONS_BASE]
        filter_chain = build_ffmpeg_filter_chain(state)
        if filter_chain:
            escaped_chain = filter_chain.replace('"', '\\"')
            options.append(f'-af "{escaped_chain}"')
        return " ".join(options)

    def _format_playback_failure_message(self, track: Track, exc: Exception) -> str:
        if isinstance(exc, PlaybackError):
            return f"No pude reproducir **{track.title}**: {exc} Pase al siguiente tema."
        return f"No pude reproducir **{track.title}** por un fallo interno. Pase al siguiente tema."

    def _ffmpeg_proxy_url(self) -> str | None:
        proxy_url = self.audio_service.proxy_override
        if not proxy_url:
            return None
        return proxy_url

    def _resolve_waiter(self, waiter: asyncio.Future[None] | None, state: GuildMusicState, field_name: str) -> None:
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        if getattr(state, field_name) is waiter:
            setattr(state, field_name, None)

    def _invalidate_track_stream_cache(self, track: Track) -> None:
        track.stream_url = None
        track.stream_headers.clear()

    def _is_local_stream(self, stream_url: str | None) -> bool:
        if not stream_url:
            return False
        if "://" in stream_url:
            return False
        return Path(stream_url).exists()

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

    async def _recover_blocked_youtube_candidate(
        self,
        state: GuildMusicState,
        failed_track: Track,
        error: Exception,
    ) -> Track | None:
        if failed_track.source != "youtube":
            return None

        query = failed_track.search_query or failed_track.title
        if not query or is_url(query):
            query = self._build_autoplay_query(failed_track)

        requester_name = failed_track.requester_name or "YouTube Recovery"
        requester_id = failed_track.requester_id or (self.bot.user.id if self.bot.user else 0)
        try:
            candidates = await self.audio_service.search_tracks(
                query=query,
                requester_name=requester_name,
                requester_id=requester_id,
                limit=max(3, self.settings.play_candidate_limit),
                source="youtube",
            )
        except Exception:
            self.logger.exception("No pude buscar candidatos alternos de YouTube para %s", failed_track.title)
            return None

        skipped_urls = {failed_track.webpage_url}
        skipped_urls.update(track.webpage_url for track in state.history)
        skipped_urls.update(track.webpage_url for track in state.queue)
        last_error: Exception | None = error

        for candidate in candidates:
            if candidate.webpage_url in skipped_urls:
                continue
            candidate.requester_name = requester_name
            candidate.requester_id = requester_id
            candidate.search_query = query
            try:
                prepared = await self.audio_service.prepare_stream(candidate)
            except PlaybackError as exc:
                last_error = exc
                self.logger.info(
                    "Candidato alterno de YouTube descartado: %s (%s)",
                    candidate.title,
                    exc,
                )
                continue

            self.logger.info(
                "Recuperacion YouTube: '%s' fallo (%s), usando '%s'",
                failed_track.title,
                last_error,
                prepared.title,
            )
            return prepared

        return None

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
        state.track_messages[track.id] = ref
        if activate and state.current and state.current.id == track.id:
            await self.promote_track_message(guild_id, track, heading=heading)

    async def promote_track_message(self, guild_id: int, track: Track, *, heading: str) -> None:
        state = self.get_state(guild_id)
        target_ref = state.track_messages.get(track.id) or state.active_panel
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
                state=state,
            ),
            view=PlayerControlsView(self.bot, guild_id),
        )
        if state.active_panel_track_id and state.active_panel_track_id != track.id:
            state.track_messages.pop(state.active_panel_track_id, None)
        state.active_panel = target_ref
        state.active_panel_track_id = track.id

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
            state.active_panel_track_id = None
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
            state.active_panel_track_id = None
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
                state=state,
            ),
            view=PlayerControlsView(self.bot, guild_id),
        )
        state.active_panel_track_id = state.current.id

    async def disable_active_panel(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.active_panel is not None:
            await self._clear_message_controls(
                state.active_panel,
                finished_track=state.history[-1] if state.history else None,
            )
        if state.active_panel_track_id:
            state.track_messages.pop(state.active_panel_track_id, None)
        state.active_panel = None
        state.active_panel_track_id = None

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
