"""utils/lavalink.py — Gestion de conexion a Lavalink + circuit breaker +
fallback yt-dlp.

Estrategia de resolucion de tracks (orden):
  1. Si Lavalink responde y el youtube-plugin esta sano: usar Lavalink
     /v4/loadtracks con el identifier correspondiente.
  2. Si el youtube-plugin ha fallado 3 veces seguidas (circuit breaker
     tripped): usar yt-dlp para buscar, devolver Track con stream_url
     poblada, y Lavalink la carga como HTTP identifier.
  3. Si Lavalink esta caido del todo: nada que hacer — el bot no puede
     reproducir audio sin Lavalink.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

import aiohttp
import wavelink
from wavelink import Node, Playable, Playlist, Pool

from config import Settings
from utils.errors import PlaybackError
from utils.models import Track

if TYPE_CHECKING:
    from main import AISAKBot


class LavalinkManager:
    def __init__(self, bot: "AISAKBot", settings: Settings, logger: logging.Logger) -> None:
        self.bot = bot
        self.settings = settings
        self.logger = logger.getChild("lavalink")
        self._session: aiohttp.ClientSession | None = None
        self._node_ready = asyncio.Event()
        self._node_ready.set()  # optimism: until proven otherwise

        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_tripped_until: float = 0.0

        # Listeners para reconnect automatico
        bot.add_listener(self._on_node_ready, "on_wavelink_node_ready")
        bot.add_listener(self._on_node_disconnected, "on_wavelink_node_disconnected")

    # ------------------------------------------------------------------ #
    # HTTP session (reused)
    # ------------------------------------------------------------------ #
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # Wavelink listeners
    # ------------------------------------------------------------------ #
    async def _on_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        self._node_ready.set()
        self.bot.runtime.lavalink_connected = True
        self.logger.info(
            "Nodo Lavalink listo: resumed=%s session_id=%s",
            payload.resumed, payload.session_id,
        )

    async def _on_node_disconnected(self, payload: wavelink.NodeDisconnectedEventPayload) -> None:
        self._node_ready.clear()
        self.bot.runtime.lavalink_connected = False
        self.logger.warning("Nodo Lavalink desconectado. Reintentando en 2s...")
        asyncio.create_task(self._auto_reconnect())

    async def _auto_reconnect(self) -> None:
        await asyncio.sleep(2)
        try:
            await self.ensure_connected()
            self.logger.info("Reconexion automatica exitosa")
        except Exception as exc:
            self.logger.warning("Reconexion automatica fallo: %s", exc)

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #
    async def connect(self) -> Node:
        self._node_ready.clear()
        # wavelink 3.5.2 acepta retries como int (no None). None causaba TypeError.
        node = Node(
            uri=f"http://{self.settings.lavalink_host}:{self.settings.lavalink_port}",
            password=self.settings.lavalink_password,
            retries=5,
        )
        await Pool.connect(client=self.bot, nodes=[node])
        try:
            await asyncio.wait_for(self._node_ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            # Limpia el nodo fallido para que la proxima vez no se acumulen.
            for existing in list(Pool.nodes.values()):
                try:
                    await existing.close(eject=True)
                except Exception:
                    pass
            self._node_ready.set()
            raise RuntimeError("Lavalink no respondio 'ready' tras 30s")
        return node

    async def ensure_connected(self) -> None:
        if self._node_ready.is_set():
            try:
                Pool.get_node()
                return
            except wavelink.InvalidNodeException:
                pass

        self.logger.info("Forzando reconexion a Lavalink...")
        for node in list(Pool.nodes.values()):
            try:
                await node.close(eject=True)
            except Exception:
                pass

        last_exc: Exception | None = None
        for attempt in range(5):
            try:
                await self.connect()
                self.logger.info("Reconexion exitosa en intento %d/5", attempt + 1)
                return
            except Exception as exc:
                last_exc = exc
                self.logger.warning("Reconexion intento %d/5 fallo: %s", attempt + 1, exc)
                await asyncio.sleep(3 + attempt * 2)

        raise RuntimeError(f"No se pudo reconectar a Lavalink tras 5 intentos: {last_exc}")

    async def disconnect(self) -> None:
        for node in list(Pool.nodes.values()):
            try:
                await node.close(eject=True)
            except Exception:
                pass
        self._node_ready.clear()
        await self.close()

    # ------------------------------------------------------------------ #
    # REST helpers
    # ------------------------------------------------------------------ #
    async def _rest_request(self, method: str, path: str, **kwargs: Any) -> dict | list | None:
        uri = f"http://{self.settings.lavalink_host}:{self.settings.lavalink_port}{path}"
        headers = {"Authorization": self.settings.lavalink_password}
        headers.update(kwargs.pop("headers", {}))
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with self.session.request(method, uri, headers=headers, timeout=timeout, **kwargs) as resp:
                if resp.status == 204:
                    return None
                if resp.status >= 300:
                    body = await resp.text()
                    self.logger.error("Lavalink REST %d en %s: %s", resp.status, path, body[:300])
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.logger.warning("Lavalink REST request fallo: %s", exc)
            return None

    async def _rest_load(self, identifier: str) -> dict | None:
        encoded = quote(identifier, safe="")
        return await self._rest_request("GET", f"/v4/loadtracks?identifier={encoded}")

    async def _parse_load_response(self, data: dict, query_for_log: str) -> list[Playable] | Playlist | None:
        load_type = data.get("loadType", "empty")
        self.logger.debug("loadType=%s para: %s", load_type, query_for_log[:80])

        if load_type == "track":
            return [Playable(data=data["data"])]
        if load_type == "search":
            tracks_data = data["data"]
            if isinstance(tracks_data, dict):
                tracks_list = tracks_data.get("tracks", [])
            else:
                tracks_list = tracks_data
            self.logger.info("Search OK: %d resultados para: %s", len(tracks_list), query_for_log[:80])
            return [Playable(data=t) for t in tracks_list]
        if load_type == "playlist":
            return Playlist(data=data["data"])
        if load_type == "error":
            err = data.get("data", {})
            self.logger.error(
                "Lavalink load error para %s: %s",
                query_for_log[:80], str(err)[:400],
            )
            self._register_failure()
            return None
        # empty
        self.logger.warning("loadType=%s (sin resultados) para: %s", load_type, query_for_log[:80])
        return []

    async def _rest_fetch_tracks(self, query: str) -> list[Playable] | Playlist | None:
        data = await self._rest_load(query)
        if data is None:
            return None
        try:
            return await self._parse_load_response(data, query)
        except Exception as exc:
            self.logger.error("Error parseando respuesta Lavalink: %s", exc)
            return None

    async def _retry_rest_fetch(self, query: str) -> list[Playable] | Playlist | None:
        for attempt in range(3):
            result = await self._rest_fetch_tracks(query)
            if result is not None:
                return result
            self.logger.warning("REST fetch fallo (intento %d/3)", attempt + 1)
            if attempt < 2:
                await asyncio.sleep(1.5)
        self.logger.error("REST fetch agoto 3 intentos para: %s", query[:80])
        return None

    # ------------------------------------------------------------------ #
    # Circuit breaker
    # ------------------------------------------------------------------ #
    def _register_failure(self) -> None:
        self._consecutive_failures += 1
        threshold = max(1, self.settings.breaker_threshold)
        cooldown = max(1, self.settings.breaker_cooldown)
        if self._consecutive_failures >= threshold:
            self._breaker_tripped_until = time.monotonic() + float(cooldown)
            self.logger.error(
                "Circuit breaker TRIPPED: youtube-plugin ha fallado %d veces. "
                "Enrutando busquedas via yt-dlp por %ds.",
                self._consecutive_failures, cooldown,
            )

    def _register_success(self) -> None:
        if self._consecutive_failures > 0:
            self._consecutive_failures = 0
            self.logger.info("Circuit breaker reseteado.")

    def breaker_is_tripped(self) -> bool:
        if self._breaker_tripped_until == 0.0:
            return False
        if time.monotonic() > self._breaker_tripped_until:
            self._breaker_tripped_until = 0.0
            self.logger.info("Circuit breaker cooldown terminado.")
            return False
        return True

    # ------------------------------------------------------------------ #
    # Public track-loading API
    # ------------------------------------------------------------------ #
    async def fetch_tracks(
        self,
        query: str,
        requester_name: str,
        requester_id: int,
        limit: int = 5,
    ) -> list[Track]:
        is_url_query = _is_url(query)

        # Path A: circuit breaker tripped + URL query → yt-dlp extract directo.
        # (Si es free query, no probamos yt-dlp search porque YouTube lo
        # bloquea desde IPs de datacenter — solo URLs funcionan.)
        if self.breaker_is_tripped() and is_url_query:
            direct = await self.ytdlp_extract_direct(query)
            if direct and direct.get("url"):
                from utils.models import Track
                from uuid import uuid4
                t = Track(
                    title=direct.get("title", "Unknown"),
                    webpage_url=direct.get("webpage_url") or query,
                    duration=direct.get("duration", 0),
                    uploader=direct.get("uploader", "Unknown"),
                    thumbnail=direct.get("thumbnail"),
                    search_query=query,
                    requester_name=requester_name,
                    requester_id=requester_id,
                    id=uuid4().hex,
                    stream_url=direct["url"],
                )
                self.logger.info("yt-dlp fallback OK para URL: %s", query[:80])
                return [t]

        # Path B: Lavalink /v4/loadtracks (youtube-plugin).
        identifier = query if is_url_query else f"ytsearch:{query}"
        result = await self._retry_rest_fetch(identifier)
        if result is None:
            # Path C: yt-dlp extract (solo para URLs, no para searches).
            if is_url_query:
                self.logger.warning("youtube-plugin fallo. Probando yt-dlp extract.")
                direct = await self.ytdlp_extract_direct(query)
                if direct and direct.get("url"):
                    from utils.models import Track
                    from uuid import uuid4
                    t = Track(
                        title=direct.get("title", "Unknown"),
                        webpage_url=direct.get("webpage_url") or query,
                        duration=direct.get("duration", 0),
                        uploader=direct.get("uploader", "Unknown"),
                        thumbnail=direct.get("thumbnail"),
                        search_query=query,
                        requester_name=requester_name,
                        requester_id=requester_id,
                        id=uuid4().hex,
                        stream_url=direct["url"],
                    )
                    return [t]
            raise PlaybackError(
                "No se pudieron obtener resultados. YouTube esta bloqueando las peticiones. "
                "Verifica que YOUTUBE_OAUTH_REFRESH_TOKEN este configurado en Render. "
                "Si lo esta, intenta de nuevo en 1 minuto o usa un enlace directo de YouTube."
            )

        tracks: list[Track] = []
        if isinstance(result, Playlist):
            for p in list(result.tracks)[:limit]:
                tracks.append(_playable_to_track(p, query, requester_name, requester_id))
            if not tracks:
                raise PlaybackError("La playlist no contiene canciones.")
            self.logger.info("Playlist '%s' cargada: %d canciones", result.name, len(tracks))
            self._register_success()
        else:
            for p in list(result)[:limit]:
                tracks.append(_playable_to_track(p, query, requester_name, requester_id))
            if not tracks:
                raise PlaybackError("No se encontraron canciones.")
            self._register_success()

        return tracks

    async def search_tracks(
        self,
        query: str,
        requester_name: str,
        requester_id: int,
        limit: int = 5,
    ) -> list[Track]:
        """Igual que fetch_tracks pero nunca lanza — para autocomplete."""
        try:
            result = await self._retry_rest_fetch(f"ytsearch:{query}")
        except Exception:
            return []
        if result is None:
            return []

        playables: list[Playable] = []
        if isinstance(result, Playlist):
            playables = list(result.tracks)[:limit]
        else:
            playables = list(result)[:limit]

        tracks = [_playable_to_track(p, query, requester_name, requester_id) for p in playables]
        if tracks:
            self._register_success()
        return tracks

    # ------------------------------------------------------------------ #
    # yt-dlp fallback
    # ------------------------------------------------------------------ #
    async def _ytdlp_search(
        self,
        query: str,
        requester_name: str,
        requester_id: int,
        limit: int,
    ) -> list[Track]:
        timeout = float(self.settings.extract_timeout)
        try:
            entries = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, _ytdlp_search_sync, query, limit
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.logger.error("yt-dlp search timeout: %s", query[:80])
            return []
        except Exception as exc:
            self.logger.warning("yt-dlp search fallo: %s", exc)
            return []

        tracks: list[Track] = []
        for entry in entries:
            tracks.append(Track(
                title=entry.get("title", "Unknown"),
                webpage_url=entry.get("webpage_url") or entry.get("url") or "",
                duration=entry.get("duration") or 0,
                uploader=entry.get("uploader") or entry.get("channel") or "Unknown",
                thumbnail=entry.get("thumbnail"),
                search_query=query,
                requester_name=requester_name,
                requester_id=requester_id,
                id=uuid4().hex,
                stream_url=entry.get("url"),
            ))
        return tracks

    async def ytdlp_extract_direct(self, webpage_url: str) -> dict | None:
        """Pide a yt-dlp un stream URL directo para un video."""
        timeout = float(self.settings.extract_timeout)
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, _ytdlp_extract_sync, webpage_url
                ),
                timeout=timeout,
            )
        except Exception as exc:
            self.logger.warning("yt-dlp extract fallo para %s: %s", webpage_url, exc)
            return None

    # ------------------------------------------------------------------ #
    # Resolucion de Playable para reproduccion
    # ------------------------------------------------------------------ #
    async def resolve_playable(self, track: Track) -> Playable | None:
        """Devuelve un Playable listo para player.play(). Estrategia:
            1. Si track._playable ya existe (viene cacheado del fetch), usarlo.
            2. Si track.stream_url existe (viene de yt-dlp), cargarlo como
               HTTP identifier en Lavalink.
            3. Si la youtube-plugin ya fallo para esta URL (circuit breaker),
               ir directo a yt-dlp extract.
            4. Sino, pedir a Lavalink que resuelva track.webpage_url via
               youtube-plugin; si falla, intentar yt-dlp extract como
               ultimo recurso.
        """
        if track._playable is not None:
            return track._playable

        # Path 2: yt-dlp stream URL directo.
        if track.stream_url:
            playable = await self._load_http_identifier(track.stream_url)
            if playable is not None:
                track._playable = playable
                return playable
            # URL stale (~6h), re-extraer.
            self.logger.info("stream_url stale para %s, re-extrayendo", track.title[:60])
            direct = await self.ytdlp_extract_direct(track.webpage_url)
            if direct and direct.get("url"):
                track.stream_url = direct["url"]
                playable = await self._load_http_identifier(direct["url"])
                if playable is not None:
                    track._playable = playable
                    return playable

        url = track.webpage_url
        if not url:
            return None

        # Path 3: si circuit breaker ya esta disparado, no insistir con plugin.
        if self.breaker_is_tripped():
            self.logger.info("Breaker tripped. yt-dlp directo para %s", url[:80])
            direct = await self.ytdlp_extract_direct(url)
            if direct and direct.get("url"):
                track.stream_url = direct["url"]
                track.webpage_url = direct.get("webpage_url") or url
                playable = await self._load_http_identifier(direct["url"])
                if playable is not None:
                    track._playable = playable
                    return playable
            return None

        # Path 4: youtube-plugin (una sola vez; sin reintentos locales).
        result = await self._rest_fetch_tracks(url)
        if result is None or (isinstance(result, list) and not result):
            # Plugin no devolvio nada util. Caer al fallback yt-dlp una sola vez.
            self.logger.info("Plugin sin resultados para %s. Probando yt-dlp.", url[:80])
            direct = await self.ytdlp_extract_direct(url)
            if direct and direct.get("url"):
                track.stream_url = direct["url"]
                track.webpage_url = direct.get("webpage_url") or url
                playable = await self._load_http_identifier(direct["url"])
                if playable is not None:
                    track._playable = playable
                    return playable
            return None

        if isinstance(result, Playlist):
            return result.tracks[0] if result.tracks else None
        return result[0] if result else None

    async def _load_http_identifier(self, stream_url: str) -> Playable | None:
        """Pide a Lavalink que cargue una URL directa como HTTP stream."""
        data = await self._rest_load(stream_url)
        if data is None:
            return None
        if data.get("loadType") == "track":
            return Playable(data=data["data"])
        self.logger.warning(
            "_load_http_identifier: loadType=%s no es track para %s",
            data.get("loadType"), stream_url[:80],
        )
        return None

    # ------------------------------------------------------------------ #
    # Warmup + health
    # ------------------------------------------------------------------ #
    async def warmup(self) -> str | None:
        """Prueba el youtube-plugin con URL + search. Devuelve:
            "ok"       — URL y search funcionan.
            "url_only" — solo URLs funcionan.
            "ytdlp"    — plugin caido, fallback funciona.
            None       — nada funciona.
        """
        # Test URL
        url_ok = False
        try:
            r = await self._rest_fetch_tracks("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            if r:
                url_ok = True
                self.logger.info("Warmup URL OK")
        except Exception as exc:
            self.logger.debug("Warmup URL fallo: %s", exc)

        # Test search
        search_ok = False
        try:
            r = await self._rest_fetch_tracks("ytsearch:never gonna give you up")
            if r and len(r) > 0:
                search_ok = True
                self.logger.info("Warmup SEARCH OK")
        except Exception as exc:
            self.logger.warning("Warmup SEARCH fallo: %s", exc)

        if url_ok and search_ok:
            return "ok"
        if url_ok:
            return "url_only"

        # Test yt-dlp
        tracks = await self._ytdlp_search("never gonna give you up", "warmup", 0, 1)
        if tracks:
            self.logger.info("Warmup yt-dlp OK")
            return "ytdlp"
        return None

    async def check_health(self) -> bool:
        data = await self._rest_request("GET", "/v4/info")
        healthy = data is not None
        self.bot.runtime.lavalink_connected = healthy
        return healthy


    # ------------------------------------------------------------------ #
    # Resource cleanup (MEJORA AGREGADA)
    # ------------------------------------------------------------------ #
    async def cleanup(self) -> None:
        """Limpia recursos del LavalinkManager.
        
        Cierra la sesión HTTP y libera referencias para evitar memory leaks.
        Debe llamarse cuando el bot se está cerrando.
        """
        # Cerrar sesión HTTP
        await self.close()
        
        # Limpiar referencias circulares
        self._node_ready.clear()
        self._consecutive_failures = 0
        self._breaker_tripped_until = 0.0
        
        self.logger.info("LavalinkManager limpiado correctamente")
    
    async def get_memory_usage(self) -> dict:
        """Retorna información sobre el uso de memoria del manager.
        
        Útil para diagnóstico y monitoreo.
        """
        import sys
        
        # Estimación básica del tamaño de objetos en memoria
        session_size = sys.getsizeof(self._session) if self._session else 0
        
        return {
            "http_session_active": self._session is not None and not self._session.closed,
            "http_session_size_bytes": session_size,
            "circuit_breaker_failures": self._consecutive_failures,
            "breaker_tripped": self.breaker_is_tripped(),
        }

# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _playable_to_track(p: Playable, search_query: str, requester_name: str, requester_id: int) -> Track:
    return Track(
        title=p.title or "Unknown",
        webpage_url=p.uri or "",
        duration=p.length // 1000 if p.length else 0,
        uploader=p.author or "Unknown",
        # wavelink 3.x: el atributo de miniatura es `artwork` (no artwork_url).
        thumbnail=getattr(p, "artwork", None),
        search_query=search_query,
        requester_name=requester_name,
        requester_id=requester_id,
        id=uuid4().hex,
        _playable=p,
    )


def _is_url(value: str) -> bool:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(value)
        return bool(parsed.scheme and parsed.netloc)
    except ValueError:
        return False


# ---------------------------------------------------------------------- #
# yt-dlp helpers (sync — corren en executor)
# ---------------------------------------------------------------------- #
def _ytdlp_build_opts() -> dict:
    """Opciones de yt-dlp afinadas para extraer desde IPs de datacenter.
    El cliente tv_embedded es el unico que sigue funcionando sin cookies
    ni PO tokens en junio 2026.
    """
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "no_write_playlist_metafiles": True,
        "socket_timeout": 20,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded"],
                "player_skip": ["webpage", "configs"],
            },
        },
        "postprocessors": [],
        "noprogress": True,
        "no_color": True,
    }


def _ytdlp_search_sync(query: str, limit: int) -> list[dict]:
    """Busca en YouTube con yt-dlp y devuelve N resultados.

    Usa extract_flat para NO descargar metadatos de cada video (eso tarda
    10-30s y dispara rate limits desde IPs de datacenter). Con extract_flat
    obtenemos solo webpage_url + titulo en ~2s. El stream URL real se
    resuelve bajo demanda en resolve_playable() cuando el track se elige.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return []
    opts = _ytdlp_build_opts()
    opts["extract_flat"] = "in_playlist"  # rapido: sin resolver cada video
    opts["playlistend"] = limit
    results: list[dict] = []
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            if not info or "entries" not in info:
                return []
            for entry in info["entries"]:
                if entry is None:
                    continue
                # En modo flat, entry tiene url = webpage_url (no stream).
                webpage_url = entry.get("url") or entry.get("webpage_url") or ""
                if not webpage_url:
                    continue
                results.append({
                    "title": entry.get("title", "Unknown"),
                    "webpage_url": webpage_url,
                    "duration": entry.get("duration") or 0,
                    "uploader": entry.get("uploader") or entry.get("channel") or entry.get("uploader_id") or "Unknown",
                    "thumbnail": entry.get("thumbnails", [{}])[0].get("url") if entry.get("thumbnails") else entry.get("thumbnail"),
                    # Sin stream_url: se resolvera via youtube-plugin o
                    # _ytdlp_extract_sync cuando este track sea el elegido.
                    "url": None,
                })
    except Exception:
        return []
    return results


def _ytdlp_extract_sync(webpage_url: str) -> dict | None:
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return None
    opts = _ytdlp_build_opts()
    opts["format"] = "bestaudio/best"
    opts["noplaylist"] = True
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            if not info:
                return None
            url = info.get("url")
            if not url:
                formats = info.get("formats") or []
                if formats:
                    audio_only = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"]
                    if audio_only:
                        audio_only.sort(key=lambda f: f.get("abr", 0), reverse=True)
                        url = audio_only[0]["url"]
                    else:
                        url = formats[-1]["url"]
            if not url:
                return None
            return {
                "url": url,
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader") or info.get("channel") or "Unknown",
                "thumbnail": info.get("thumbnail"),
                "webpage_url": info.get("webpage_url") or webpage_url,
            }
    except Exception:
        return None
