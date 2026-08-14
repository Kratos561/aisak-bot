"""utils/music.py — Estado de reproduccion + logica de cola/playback.

Centraliza toda la logica que no es de Discord UI: enqueue, skip, pause,
stop, autoplay, filtros, etc.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import discord
import wavelink

from config import Settings
from utils.errors import PlaybackError, PermissionError, UserInputError
from utils.models import FilterPreset, GuildMusicState, RepeatMode, Track
from utils.ui import (
    PlayerControlsView,
    error_embed,
    now_playing_embed,
    queue_embed,
    success_embed,
    track_embed,
)
from utils.validators import is_connected, is_url

if TYPE_CHECKING:
    from main import AISAKBot


# Presets de EQ por banda. La banda 0 es la mas grave (25Hz), la 15 la mas
# aguda (16kHz). Cada entrada es {"band": int, "gain": float en [-0.25, 0.25].
EQ_PRESETS: dict[FilterPreset, list[dict[str, float]]] = {
    FilterPreset.BASSBOOST: [
        {"band": 0, "gain": 0.2},
        {"band": 1, "gain": 0.15},
        {"band": 2, "gain": 0.1},
        {"band": 3, "gain": 0.05},
    ],
    FilterPreset.CLEAR: [
        {"band": 0, "gain": -0.1},
        {"band": 1, "gain": 0.05},
        {"band": 2, "gain": 0.1},
        {"band": 3, "gain": 0.15},
        {"band": 4, "gain": 0.1},
    ],
    FilterPreset.RADIO: [
        {"band": 0, "gain": -0.15},
        {"band": 1, "gain": 0.0},
        {"band": 2, "gain": 0.15},
        {"band": 3, "gain": 0.2},
        {"band": 4, "gain": 0.15},
    ],
    # NIGHTCORE y VAPORWAVE se implementan solo con timescale (sin EQ).
    FilterPreset.NIGHTCORE: [],
    FilterPreset.VAPORWAVE: [],
}


class MusicManager:
    def __init__(self, bot: "AISAKBot", settings: Settings, logger: logging.Logger) -> None:
        self.bot = bot
        self.settings = settings
        self.logger = logger.getChild("music")
        self.states: dict[int, GuildMusicState] = {}
        # Mensajes pendientes de enviar al canal de texto. Se envian al final
        # del ciclo de carga para evitar race con _after_playback.
        self._notify_pending: dict[int, str] = {}

        bot.add_listener(self._on_track_end, "on_wavelink_track_end")
        bot.add_listener(self._on_track_exception, "on_wavelink_track_exception")
        bot.add_listener(self._on_inactive_player, "on_wavelink_inactive_player")
        bot.add_listener(self._on_track_start, "on_wavelink_track_start")

    @property
    def lavalink(self):
        return self.bot.lavalink

    # ------------------------------------------------------------------ #
    # Wavelink event handlers
    # ------------------------------------------------------------------ #
    async def _on_track_start(self, player: wavelink.Player, track: wavelink.Playable) -> None:
        self.logger.info(
            "TRACK START guild=%s track=%s uri=%s",
            player.guild.id, getattr(track, "title", "?"), getattr(track, "uri", "?"),
        )

    async def _on_track_end(self, player: wavelink.Player, track: wavelink.Playable, reason: str) -> None:
        guild_id = player.guild.id
        state = self.states.get(guild_id)
        if state is None:
            return
        self.logger.info(
            "Track end guild=%s reason=%s playing=%s pos=%s",
            guild_id, reason, player.playing, player.position,
        )
        # wavelink reasons: finished, loadFailed, stopped, replaced, cleanup.
        # "replaced"/"cleanup" son internos (track sobreescrito o player cerrado):
        # no avanzar cola. "stopped" viene de player.stop() manual — nuestro
        # manual_skip/manual_stop ya manejan el avance. Cualquier otra razon
        # (finished, loadFailed) avanza la cola normalmente.
        if reason in {"replaced", "cleanup"}:
            return
        await self._after_playback(guild_id, reason)

    async def _on_track_exception(
        self,
        player: wavelink.Player,
        track: wavelink.Playable,
        exception: Exception,
    ) -> None:
        guild_id = player.guild.id
        state = self.states.get(guild_id)
        if state is None:
            return
        self.logger.error(
            "Track exception guild=%s: %s: %s",
            guild_id, type(exception).__name__, exception,
        )
        # IMPORTANTE: wavelink dispara on_wavelink_track_end con reason="loadFailed"
        # DESPUES de on_wavelink_track_exception. Por eso aqui SOLO registramos
        # el mensaje para el usuario; el skip real lo hace _on_track_end via
        # _after_playback(reason="load_failed"). Si llamaramos _after_playback
        # aqui tambien, saltariamos DOS canciones por cada fallo.
        self._notify_pending[guild_id] = (
            f"Error reproduciendo **{getattr(track, 'title', '?')}** ({type(exception).__name__}). "
            f"Saltando."
        )
        # Forzar el track_end: en algunas versiones de wavelink la excepcion no
        # siempre dispara track_end, asi que invocamos player.stop() para
        # garantizar el avance. _on_track_end respetara el reason correcto.
        try:
            await player.stop()
        except Exception as exc:
            self.logger.warning("player.stop() post-exception fallo: %s", exc)

    async def _on_inactive_player(self, player: wavelink.Player) -> None:
        guild_id = player.guild.id
        state = self.states.get(guild_id)
        # No desconectar si hay un panel activo que queramos mantener, o si
        # el usuario acaba de encolar otra cancion (panel activo indica uso).
        if state and state.active_panel is not None:
            return
        self.logger.info("Player inactivo guild=%s. Desconectando.", guild_id)
        if state:
            await self._notify(state, "Inactividad prolongada. Me desconecte.")
        await self.disconnect(guild_id)

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState(
                guild_id=guild_id,
                volume=self.settings.default_volume_ratio,
            )
        return self.states[guild_id]

    # ------------------------------------------------------------------ #
    # Voice connection
    # ------------------------------------------------------------------ #
    async def ensure_voice(self, interaction: discord.Interaction) -> GuildMusicState:
        guild = interaction.guild
        if guild is None:
            raise UserInputError("Este comando solo funciona dentro de un servidor.")

        voice_state = getattr(interaction.user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            raise UserInputError("Debes estar en un canal de voz.")

        channel = voice_state.channel
        me = guild.me
        if me is None:
            raise PermissionError("No pude verificar mis permisos en este servidor.")

        perms = channel.permissions_for(me)
        if not perms.connect or not perms.speak:
            raise PermissionError("Necesito permisos de `Connect` y `Speak` en ese canal.")

        state = self.get_state(guild.id)
        state.text_channel_id = interaction.channel_id

        async with state.connect_lock:
            current_vc = guild.voice_client

            if current_vc and not is_connected(current_vc):
                self.logger.warning("Voice client zombie en guild=%s; limpiando.", guild.id)
                try:
                    await current_vc.disconnect(force=True)
                except Exception:
                    pass
                current_vc = None

            if current_vc and current_vc.channel != channel:
                raise PermissionError(
                    "Ya estoy en otro canal de voz. Entra al mismo canal que yo."
                )
            elif current_vc is None:
                for attempt in range(3):
                    try:
                        await self.lavalink.ensure_connected()
                        self.logger.info(
                            "Conectando voz guild=%s canal=%s", guild.id, channel.id
                        )
                        player = await channel.connect(cls=wavelink.Player, self_deaf=True)
                        state.player = player
                        break
                    except (wavelink.InvalidNodeException, wavelink.ChannelTimeoutException, RuntimeError) as exc:
                        self.logger.warning("Voz intento %d/3 fallo: %s", attempt + 1, exc)
                        if attempt == 2:
                            raise PlaybackError(
                                "No pude conectar al canal de voz. El servidor de audio no responde. "
                                "Intentalo de nuevo en unos segundos."
                            )
                        partial = guild.voice_client
                        if partial:
                            try:
                                await partial.disconnect(force=True)
                            except Exception:
                                pass
                        await asyncio.sleep(2)
            else:
                state.player = current_vc

        await self._cancel_auto_disconnect(state)
        return state

    def assert_control_access(self, interaction: discord.Interaction) -> GuildMusicState:
        guild = interaction.guild
        if guild is None:
            raise UserInputError("Esto solo funciona en un servidor.")
        state = self.get_state(guild.id)
        player = state.player or guild.voice_client
        if player is None or not is_connected(player):
            raise UserInputError("No hay sesion de voz activa.")
        user_voice = getattr(interaction.user, "voice", None)
        user_channel = getattr(user_voice, "channel", None)
        if user_channel is None:
            raise UserInputError("Debes estar en el mismo canal de voz que el bot.")
        bot_channel = getattr(player, "channel", None)
        if bot_channel and getattr(bot_channel, "id", None) != getattr(user_channel, "id", None):
            raise PermissionError(f"Debes estar en **{bot_channel.name}** para controlar la reproduccion.")
        state.player = player
        state.text_channel_id = interaction.channel_id
        return state

    # ------------------------------------------------------------------ #
    # Queue + playback
    # ------------------------------------------------------------------ #
    async def enqueue_query(self, interaction: discord.Interaction, query: str) -> list[Track]:
        state = await self.ensure_voice(interaction)
        # Si la URL apunta a una lista de reproduccion (contiene ?list= o
        # /playlist), expandir; si es free query y cola vacia, buscar N
        # candidatos y auto-seleccionar el primero reproducible.
        is_url_query = is_url(query)
        is_playlist_url = is_url_query and (
            "list=" in query
            or "/playlist" in query.lower()
        )

        if is_playlist_url:
            limit = self._available_slots(state)
        elif not is_url_query and not state.queue and not self.is_playing(state.guild_id):
            limit = min(self._available_slots(state), self.settings.search_limit)
        else:
            limit = 1

        try:
            await self.lavalink.ensure_connected()
        except Exception as exc:
            self.logger.warning("ensure_connected pre-fetch fallo: %s", exc)

        tracks = await self.lavalink.fetch_tracks(
            query=query,
            requester_name=interaction.user.display_name,
            requester_id=interaction.user.id,
            limit=limit,
        )

        if not is_url_query and not is_playlist_url and not state.queue and not self.is_playing(state.guild_id):
            # Auto-seleccionar el primer track reproducible.
            tracks = [await self._select_first_playable(tracks)]

        return await self.enqueue_tracks(interaction, tracks)

    async def enqueue_tracks(self, interaction: discord.Interaction, tracks: list[Track]) -> list[Track]:
        state = await self.ensure_voice(interaction)
        if not tracks:
            raise PlaybackError("No hay canciones para agregar.")
        available = self._available_slots(state)
        accepted = tracks[:available]
        if not accepted:
            raise UserInputError("La cola esta llena.")
        for t in accepted:
            state.queue.append(t)
        if not self.is_playing(state.guild_id):
            await self.start_next(state.guild_id)
        return accepted

    async def search(self, interaction: discord.Interaction, query: str, limit: int = 5) -> list[Track]:
        await self.ensure_voice(interaction)
        return await self.lavalink.search_tracks(
            query=query,
            requester_name=interaction.user.display_name,
            requester_id=interaction.user.id,
            limit=limit,
        )

    async def start_next(self, guild_id: int, *, heading: str = "Reproduccion iniciada") -> Track | None:
        state = self.get_state(guild_id)
        if state.play_lock.locked() and state.play_lock_owner is asyncio.current_task():
            # Re-entrada: ya estamos dentro de un start_next anterior
            # (caso tipico: skip() durante una carga). Salir silenciosamente.
            return None
        async with state.play_lock:
            state.play_lock_owner = asyncio.current_task()
            try:
                return await self._start_next_locked(guild_id, heading=heading)
            finally:
                state.play_lock_owner = None

    async def _start_next_locked(self, guild_id: int, *, heading: str) -> Track | None:
        state = self.get_state(guild_id)
        # Drenar mensajes pendientes acumulados por _on_track_exception.
        pending = self._notify_pending.pop(guild_id, None)
        if pending:
            await self._notify(state, pending)
        player = state.player
        if player is None or not is_connected(player):
            self.logger.warning("start_next: sin voz en guild=%s", guild_id)
            state.current = None
            return None

        await self._cancel_auto_disconnect(state)

        while state.queue:
            track = state.queue.popleft()
            try:
                playable = await self.lavalink.resolve_playable(track)
                if playable is None:
                    raise PlaybackError("No se pudo obtener el audio de la pista.")

                self.logger.info(
                    "PLAY guild=%s track=%s uri=%s encoded=%s",
                    guild_id, track.title[:60],
                    getattr(playable, "uri", "?"),
                    "yes" if getattr(playable, "encoded", None) else "no",
                )

                state.current = track
                state.reset_progress()

                try:
                    await player.play(playable)
                except Exception as exc:
                    self.logger.exception("player.play() RAISED: %s", exc)
                    raise

                # NO usamos sleep(1.5): los eventos on_wavelink_track_start /
                # on_wavelink_track_end llegan asincronicamente. Forzar un sleep
                # introducia una race condition donde si el track terminaba
                # durante el sleep, el siguiente _after_playback se ejecutaba
                # antes de que este _start_next_locked retornara, causando
                # re-entrada y posibles dobles reproducciones.
                self.logger.info(
                    "POST-PLAY guild=%s playing=%s paused=%s pos=%s connected=%s",
                    guild_id, player.playing, player.paused,
                    player.position, player.connected,
                )

                await self._set_volume(state)
                try:
                    await self._apply_filters(state)
                except Exception:
                    self.logger.exception("Filtros fallaron en guild=%s", guild_id)

                try:
                    await self._activate_panel(guild_id, track, heading)
                except Exception:
                    self.logger.exception("Panel no se pudo activar en guild=%s", guild_id)

                return track
            except Exception as exc:
                self.logger.exception("Fallo reproduciendo %s", track.title[:60])
                await self._notify(state, f"No pude reproducir **{track.title}**: {exc}. Siguiente.")
                heading = "Reproduccion iniciada"
                continue

        state.current = None
        state.clear_progress()
        await self._deactivate_panel(guild_id)
        await self._schedule_auto_disconnect(state)
        return None

    async def _select_first_playable(self, candidates: list[Track]) -> Track:
        """Devuelve el primer track que el manager logra resolver como Playable.

        Para minimizar la latencia (importante en /play con texto libre),
        probamos todos los candidatos en paralelo. El primero que resuelva
        gana; los demas se cancelan.
        """
        if not candidates:
            raise PlaybackError("No encontre canciones para reproducir.")

        async def try_resolve(idx: int, c: Track) -> tuple[int, Track | None]:
            try:
                p = await self.lavalink.resolve_playable(c)
            except Exception:
                return idx, None
            return idx, p if p is not None else None

        tasks = [asyncio.create_task(try_resolve(i, c)) for i, c in enumerate(candidates)]
        try:
            for fut in asyncio.as_completed(tasks):
                idx, playable = await fut
                if playable is not None:
                    c = candidates[idx]
                    c._playable = playable
                    return c
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        raise PlaybackError("Ningun candidato era reproducible.")

    async def _after_playback(self, guild_id: int, reason: str) -> None:
        state = self.get_state(guild_id)
        current = state.current

        if state.manual_stop:
            state.manual_stop = False
            state.current = None
            state.clear_progress()
            await self.disconnect(guild_id)
            return

        if state.manual_skip:
            state.manual_skip = False
            state.current = None
            state.clear_progress()
            if current:
                state.history.append(current)
            await self.start_next(guild_id)
            return

        if current:
            if state.repeat_mode == RepeatMode.ONE:
                state.queue.appendleft(current)
            else:
                state.history.append(current)
                if state.repeat_mode == RepeatMode.ALL:
                    state.queue.append(current)

        state.current = None
        state.clear_progress()

        # AutoPlay
        if not state.queue and state.autoplay_enabled and current:
            await self._enqueue_autoplay(state, current)

        await self.start_next(guild_id)

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #
    async def pause(self, guild_id: int) -> Track:
        state = self.get_state(guild_id)
        player = state.player
        if not player or not player.playing or state.current is None:
            raise PlaybackError("No hay nada reproduciendose.")
        await player.pause()
        state.paused_at = discord.utils.utcnow()
        return state.current

    async def resume(self, guild_id: int) -> Track:
        state = self.get_state(guild_id)
        player = state.player
        if not player or not player.paused or state.current is None:
            raise PlaybackError("No hay nada pausado.")
        if state.paused_at:
            state.paused_seconds += (discord.utils.utcnow() - state.paused_at).total_seconds()
            state.paused_at = None
        # wavelink 3.x elimino player.resume(): se reanuda con pause(False).
        await player.pause(False)
        return state.current

    async def skip(self, guild_id: int, count: int = 1) -> Track | None:
        state = self.get_state(guild_id)
        if count > 1:
            for _ in range(max(0, count - 1)):
                if state.queue:
                    state.queue.popleft()
        player = state.player
        if player and (player.playing or player.paused):
            state.manual_skip = True
            current = state.current
            await player.stop()
            return current
        if state.queue:
            return await self.start_next(guild_id)
        raise PlaybackError("No hay canciones para saltar.")

    async def stop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        state.queue.clear()
        state.repeat_mode = RepeatMode.OFF
        player = state.player
        if player and (player.playing or player.paused):
            state.manual_stop = True
            await player.stop()
            return
        await self._deactivate_panel(guild_id)
        await self.disconnect(guild_id)

    async def disconnect(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        await self._cancel_auto_disconnect(state)
        await self._deactivate_panel(guild_id)
        player = state.player
        if player and is_connected(player):
            try:
                await player.disconnect(force=False)
            except Exception:
                pass
        state.player = None
        state.current = None
        state.queue.clear()
        state.clear_progress()
        state.manual_skip = False
        state.manual_stop = False

    def remove_from_queue(self, guild_id: int, position: int) -> Track:
        state = self.get_state(guild_id)
        items = list(state.queue)
        if position < 1 or position > len(items):
            raise UserInputError("Esa posicion no existe en la cola.")
        removed = items.pop(position - 1)
        state.queue.clear()
        state.queue.extend(items)
        return removed

    def clear_queue(self, guild_id: int) -> int:
        state = self.get_state(guild_id)
        n = len(state.queue)
        state.queue.clear()
        return n

    def shuffle_queue(self, guild_id: int) -> int:
        state = self.get_state(guild_id)
        items = list(state.queue)
        random.shuffle(items)
        state.queue.clear()
        state.queue.extend(items)
        return len(items)

    def set_repeat(self, guild_id: int, mode: RepeatMode) -> RepeatMode:
        state = self.get_state(guild_id)
        state.repeat_mode = mode
        return mode

    def toggle_autoplay(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        state.autoplay_enabled = not state.autoplay_enabled
        return state.autoplay_enabled

    # ------------------------------------------------------------------ #
    # Audio effects
    # ------------------------------------------------------------------ #
    async def set_volume(self, guild_id: int, volume: int) -> int:
        state = self.get_state(guild_id)
        state.volume = volume / 100.0
        await self._set_volume(state)
        return volume

    async def set_speed(self, guild_id: int, speed: float) -> tuple[float, bool]:
        state = self.get_state(guild_id)
        state.playback_speed = speed
        applied = await self._apply_filters(state)
        return speed, applied

    async def set_pitch(self, guild_id: int, semitones: int) -> tuple[int, bool]:
        state = self.get_state(guild_id)
        state.pitch_semitones = semitones
        applied = await self._apply_filters(state)
        return semitones, applied

    async def set_filter(self, guild_id: int, preset: FilterPreset) -> tuple[FilterPreset, bool]:
        state = self.get_state(guild_id)
        state.filter_preset = preset
        applied = await self._apply_filters(state)
        return preset, applied

    async def reset_effects(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        state.playback_speed = 1.0
        state.pitch_semitones = 0
        state.filter_preset = FilterPreset.OFF
        return await self._apply_filters(state)

    async def _set_volume(self, state: GuildMusicState) -> None:
        player = state.player
        if player is None:
            return
        vol = max(0, min(int(state.volume * 100), 100))
        if hasattr(player, "set_volume"):
            await player.set_volume(vol)
        else:
            player.volume = vol

    async def _apply_filters(self, state: GuildMusicState) -> bool:
        player = state.player
        if player is None or not getattr(player, "playing", False):
            return False
        filters = wavelink.Filters()
        speed = state.playback_speed
        pitch_mult = 2 ** (state.pitch_semitones / 12) if state.pitch_semitones else 1.0
        has_filters = False

        if state.filter_preset == FilterPreset.NIGHTCORE:
            filters.timescale.set(speed=1.3, pitch=1.3)
            has_filters = True
        elif state.filter_preset == FilterPreset.VAPORWAVE:
            filters.timescale.set(speed=0.8, pitch=0.8)
            has_filters = True
        elif speed != 1.0 or pitch_mult != 1.0:
            filters.timescale.set(speed=speed, pitch=pitch_mult)
            has_filters = True

        if state.filter_preset != FilterPreset.OFF:
            bands = EQ_PRESETS.get(state.filter_preset, [])
            if bands:
                filters.equalizer.set(bands=bands)
                has_filters = True

        if has_filters:
            await player.set_filters(filters)
        else:
            await player.set_filters(None)
        return True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def is_playing(self, guild_id: int) -> bool:
        state = self.get_state(guild_id)
        p = state.player
        if not p:
            return False
        return bool(is_connected(p) and (p.playing or p.paused))

    def _available_slots(self, state: GuildMusicState) -> int:
        current = len(state.queue) + (1 if state.current else 0)
        return max(0, self.settings.max_queue_length - current)

    async def _notify(self, state: GuildMusicState, message: str) -> None:
        if not state.text_channel_id:
            return
        channel = self.bot.get_channel(state.text_channel_id)
        if channel and hasattr(channel, "send"):
            try:
                await channel.send(message)
            except discord.HTTPException:
                pass

    async def _schedule_auto_disconnect(self, state: GuildMusicState) -> None:
        if state.auto_disconnect_task and not state.auto_disconnect_task.done():
            return

        async def worker() -> None:
            await asyncio.sleep(self.settings.inactivity_timeout)
            p = state.player
            if not p or not is_connected(p):
                return
            if p.playing or p.paused:
                return
            await self._notify(state, "Inactividad prolongada. Me desconecte.")
            await self.disconnect(state.guild_id)

        state.auto_disconnect_task = asyncio.create_task(
            worker(), name=f"aisak-idle-{state.guild_id}"
        )

    async def _cancel_auto_disconnect(self, state: GuildMusicState) -> None:
        t = state.auto_disconnect_task
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        state.auto_disconnect_task = None

    async def _enqueue_autoplay(self, state: GuildMusicState, seed: Track) -> None:
        requester_id = self.bot.user.id if self.bot.user else 0
        query = seed.search_query or seed.title
        try:
            candidates = await self.lavalink.search_tracks(
                query=query,
                requester_name="AutoPlay",
                requester_id=requester_id,
                limit=5,
            )
        except Exception:
            self.logger.exception("AutoPlay fallo buscando relacionados.")
            return
        seen = {seed.webpage_url}
        seen.update(t.webpage_url for t in state.queue)
        seen.update(t.webpage_url for t in state.history)
        for c in candidates:
            if c.webpage_url in seen:
                continue
            c.requester_name = "AutoPlay"
            c.requester_id = requester_id
            state.queue.append(c)
            await self._notify(state, f"AutoPlay agrego **{c.title}** a la cola.")
            return

    # ------------------------------------------------------------------ #
    # Panel management
    # ------------------------------------------------------------------ #
    async def _activate_panel(self, guild_id: int, track: Track, heading: str) -> None:
        state = self.get_state(guild_id)
        if state.active_panel is None:
            return
        channel = self.bot.get_channel(state.active_panel.channel_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(state.active_panel.message_id)
        except Exception:
            state.active_panel = None
            return
        try:
            await msg.edit(
                embed=track_embed(
                    track, heading, self.settings.bot_color,
                    voice_channel_name=self._voice_channel_name(state),
                    state=state,
                ),
                view=PlayerControlsView(self.bot, guild_id),
            )
        except discord.HTTPException:
            pass

    async def _deactivate_panel(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.active_panel is None:
            return
        channel = self.bot.get_channel(state.active_panel.channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(state.active_panel.message_id)
                await msg.edit(view=None)
            except Exception:
                pass
        state.active_panel = None

    async def refresh_panel(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.active_panel is None or state.current is None:
            return
        channel = self.bot.get_channel(state.active_panel.channel_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(state.active_panel.message_id)
            await msg.edit(
                embed=track_embed(
                    state.current, "Reproduciendo ahora", self.settings.bot_color,
                    voice_channel_name=self._voice_channel_name(state),
                    state=state,
                ),
                view=PlayerControlsView(self.bot, guild_id),
            )
        except Exception:
            pass

    def _voice_channel_name(self, state: GuildMusicState) -> str | None:
        ch = getattr(state.player, "channel", None) if state.player else None
        return getattr(ch, "name", None)

    # ------------------------------------------------------------------ #
    # Memory management (MEJORA AGREGADA)
    # ------------------------------------------------------------------ #
    async def cleanup_inactive_states(self) -> int:
        """Limpia estados de guilds inactivos para liberar memoria.
        
        Retorna el número de estados limpiados.
        Útil para evitar memory leaks en bots de larga duración.
        
        Criterios de limpieza:
        - No hay player activo o no está conectado
        - No hay cola ni canción actual
        - No hay panel activo
        - Última actividad fue hace más de 1 hora
        """
        cleaned = 0
        current_time = discord.utils.utcnow()
        
        guild_ids_to_remove = []
        for guild_id, state in list(self.states.items()):
            has_active_player = (state.player is not None 
                               and is_connected(state.player)
                               and (state.player.playing or state.player.paused))
            
            if (not has_active_player
                and not state.queue 
                and state.current is None
                and state.active_panel is None
                and state.started_at is not None
                and (current_time - state.started_at).total_seconds() > 3600):
                guild_ids_to_remove.append(guild_id)
        
        for guild_id in guild_ids_to_remove:
            state = self.states.pop(guild_id, None)
            if state:
                # Limpiar referencias para ayudar al garbage collector
                state.queue.clear()
                state.history.clear()
                state.current = None
                state.player = None
                cleaned += 1
                self.logger.debug(f"Limpiado estado inactivo de guild {guild_id}")
        
        if cleaned > 0:
            self.logger.info(f"Limpieza de memoria: {cleaned} estados inactivos removidos")
        
        return cleaned
