from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from pathlib import Path
from typing import Any

import yt_dlp

from config import Settings
from utils.errors import ConfigurationError, PlaybackError, UserInputError
from utils.models import Track
from utils.source_router import YOUTUBE_URL_RE, QueryPlan, build_query_plan, format_source_label
from utils.validators import is_url, sanitize_query

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:  # pragma: no cover - dependency optional in tests
    spotipy = None
    SpotifyClientCredentials = None


SPOTIFY_URL_RE = re.compile(
    r"^(?:https?://open\.spotify\.com/(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]+)|spotify:(?P<kind_uri>track|album|playlist):(?P<id_uri>[A-Za-z0-9]+))"
)
UNAVAILABLE_YOUTUBE_TITLES = {
    "[private video]",
    "[deleted video]",
}
BRACKETED_SEGMENT_RE = re.compile(r"\[[^\]]*\]|\([^\)]*\)")
NON_WORD_QUERY_RE = re.compile(r"[^0-9A-Za-zÀ-ÿ]+")
FALLBACK_STOP_TOKENS = {
    "official",
    "video",
    "audio",
    "prod",
    "slowed",
    "reverb",
    "ultra",
    "super",
    "remastered",
    "mix",
}


class AudioService:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger.getChild("audio")
        self.spotify_client = self._build_spotify_client()
        self.proxy_override = self._build_proxy_override()
        self.youtube_cookiefile = self._build_youtube_cookiefile()

    def _build_spotify_client(self) -> Any | None:
        if not self.settings.spotify_client_id or not self.settings.spotify_client_secret:
            return None
        if spotipy is None or SpotifyClientCredentials is None:
            self.logger.warning("Spotipy no esta instalado; el soporte de Spotify quedara deshabilitado.")
            return None

        auth_manager = SpotifyClientCredentials(
            client_id=self.settings.spotify_client_id,
            client_secret=self.settings.spotify_client_secret,
        )
        return spotipy.Spotify(auth_manager=auth_manager)

    def _build_proxy_override(self) -> str | None:
        proxy_candidates = [
            os.getenv(name, "").strip()
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            )
        ]
        proxy_candidates = [value for value in proxy_candidates if value]
        if not proxy_candidates:
            return None

        normalized = {value.lower().rstrip("/") for value in proxy_candidates}
        invalid_loopback_proxies = {
            "http://127.0.0.1:9",
            "https://127.0.0.1:9",
            "http://localhost:9",
            "https://localhost:9",
        }
        if normalized.issubset(invalid_loopback_proxies):
            self.logger.warning("Se detecto un proxy local invalido; yt-dlp lo ignorara para evitar fallos de red.")
            return ""
        return None

    def _build_youtube_cookiefile(self) -> str | None:
        configured_path = (self.settings.ytdlp_youtube_cookies_path or "").strip()
        if configured_path:
            path = Path(configured_path)
            if path.exists():
                self.logger.info("Cookies de YouTube habilitadas desde archivo configurado.")
                return str(path)
            self.logger.warning("YTDLP_YOUTUBE_COOKIES_PATH apunta a un archivo inexistente; se ignorara.")

        cookie_text = (self.settings.ytdlp_youtube_cookies_text or "").strip()
        cookie_b64 = (self.settings.ytdlp_youtube_cookies_b64 or "").strip()
        if cookie_b64:
            try:
                cookie_text = base64.b64decode(cookie_b64).decode("utf-8")
            except Exception:
                self.logger.exception("No pude decodificar YTDLP_YOUTUBE_COOKIES_B64; se ignoraran esas cookies.")
                cookie_text = ""

        if not cookie_text:
            return None

        if "\\n" in cookie_text and "\n" not in cookie_text:
            cookie_text = cookie_text.replace("\\n", "\n")

        cookie_path = Path("./data/cache/youtube_cookies.txt")
        try:
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(cookie_text.rstrip() + "\n", encoding="utf-8")
        except OSError:
            self.logger.exception("No pude escribir las cookies de YouTube en cache local.")
            return None

        self.logger.info("Cookies de YouTube habilitadas desde variables de entorno.")
        return str(cookie_path)

    async def fetch_tracks(
        self,
        query: str,
        requester_name: str,
        requester_id: int,
        limit: int = 1,
        source: str = "auto",
    ) -> list[Track]:
        normalized_query = sanitize_query(query)
        if match := SPOTIFY_URL_RE.match(normalized_query):
            spotify_queries = await self._run_blocking(self._resolve_spotify_queries, match)
            if not spotify_queries:
                raise PlaybackError("No pude resolver esa URL de Spotify a canciones reproducibles.")

            tracks: list[Track] = []
            spotify_source = "youtube"
            for spotify_query in spotify_queries[:limit]:
                tracks.extend(
                    await self._run_blocking(
                        self._resolve_tracks_sync,
                        build_query_plan(spotify_query, spotify_source),
                        requester_name,
                        requester_id,
                        1,
                        False,
                    )
                )
            return tracks

        plan = build_query_plan(normalized_query, source)
        return await self._run_blocking(
            self._resolve_tracks_sync,
            plan,
            requester_name,
            requester_id,
            limit,
            False,
        )

    def should_expand_query(self, query: str) -> bool:
        normalized_query = sanitize_query(query)
        spotify_match = SPOTIFY_URL_RE.match(normalized_query)
        if spotify_match:
            kind = spotify_match.group("kind") or spotify_match.group("kind_uri")
            return kind in {"album", "playlist"}

        return self._is_expandable_media_url(normalized_query)

    async def search_tracks(
        self,
        query: str,
        requester_name: str,
        requester_id: int,
        limit: int = 5,
        source: str = "auto",
    ) -> list[Track]:
        normalized_query = sanitize_query(query)
        plan = build_query_plan(normalized_query, source)
        return await self._run_blocking(
            self._resolve_tracks_sync,
            plan,
            requester_name,
            requester_id,
            limit,
            True,
        )

    async def prepare_stream(self, track: Track) -> Track:
        if track.stream_url:
            return track
        return await self._run_blocking(self._prepare_stream_sync, track)

    async def _run_blocking(self, func: Any, *args: Any) -> Any:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args),
                timeout=max(5, self.settings.ytdlp_operation_timeout),
            )
        except TimeoutError as exc:
            raise PlaybackError(
                "La fuente tardo demasiado en responder. Intenta otra busqueda o vuelve a probar en unos segundos."
            ) from exc

    def _resolve_tracks_sync(
        self,
        plan: QueryPlan,
        requester_name: str,
        requester_id: int,
        limit: int,
        combine_results: bool,
    ) -> list[Track]:
        sources = plan.search_sources if combine_results else plan.playback_sources
        failures: list[tuple[str, PlaybackError]] = []
        aggregated: list[Track] = []
        seen_urls: set[str] = set()

        for candidate_source in sources:
            remaining = limit if not combine_results else max(0, limit - len(aggregated))
            if remaining <= 0:
                break

            try:
                source_tracks = self._metadata_to_tracks(
                    plan.query,
                    requester_name,
                    requester_id,
                    remaining,
                    candidate_source,
                )
            except PlaybackError as exc:
                failures.append((candidate_source, exc))
                if plan.requested_source != "auto":
                    raise
                continue
            except Exception as exc:  # pragma: no cover - runtime/network path
                translated = self._translate_lookup_error(candidate_source, exc)
                failures.append((candidate_source, translated))
                if plan.requested_source != "auto":
                    raise translated
                continue

            if not combine_results:
                return source_tracks

            for track in source_tracks:
                if track.webpage_url in seen_urls:
                    continue
                seen_urls.add(track.webpage_url)
                aggregated.append(track)
                if len(aggregated) >= limit:
                    return aggregated

        if aggregated:
            return aggregated

        if failures:
            if plan.requested_source == "auto" and len(sources) > 1:
                names = " y ".join(format_source_label(source) for source, _ in failures)
                raise PlaybackError(f"No pude resolver esa busqueda en {names}.")
            raise failures[-1][1]

        raise PlaybackError("No encontre resultados para esa busqueda.")

    def _metadata_to_tracks(
        self,
        query: str,
        requester_name: str,
        requester_id: int,
        limit: int,
        source: str,
    ) -> list[Track]:
        direct_url = is_url(query)
        playlist_like = self._is_expandable_media_url(query)
        metadata_options = self._build_ydl_options(
            mode="metadata",
            limit=limit,
            source=source,
            direct_url=direct_url,
            playlist_like=playlist_like,
        )
        target = self._build_target(query, source, limit)
        tracks = self._extract_tracks_from_metadata(
            target=target,
            query=query,
            requester_name=requester_name,
            requester_id=requester_id,
            source=source,
            limit=limit,
            ydl_options=metadata_options,
        )

        if tracks:
            return tracks

        if playlist_like and direct_url and source == "youtube":
            self.logger.info("Playlist de YouTube sin resultados reproducibles en modo profundo; probando fallback flat.")
            fallback_options = self._build_ydl_options(
                mode="metadata",
                limit=limit,
                source=source,
                direct_url=False,
                playlist_like=True,
            )
            tracks = self._extract_tracks_from_metadata(
                target=target,
                query=query,
                requester_name=requester_name,
                requester_id=requester_id,
                source=source,
                limit=limit,
                ydl_options=fallback_options,
            )
            if tracks:
                return tracks

        if playlist_like:
            raise PlaybackError(f"No encontre canciones reproducibles en esa playlist de {format_source_label(source)}.")
        raise PlaybackError(f"No encontre resultados en {format_source_label(source)} para esa busqueda.")

    def _extract_tracks_from_metadata(
        self,
        *,
        target: str,
        query: str,
        requester_name: str,
        requester_id: int,
        source: str,
        limit: int,
        ydl_options: dict[str, Any],
    ) -> list[Track]:
        with yt_dlp.YoutubeDL(ydl_options) as downloader:
            info = downloader.extract_info(target, download=False)

        if isinstance(info, dict) and "entries" in info:
            items = [entry for entry in (info.get("entries") or []) if entry]
        else:
            items = [info] if info else []
        tracks: list[Track] = []

        for entry in items[:limit]:
            if self._should_skip_metadata_entry(entry, source):
                continue

            webpage_url = self._resolve_entry_webpage_url(entry, query)

            tracks.append(
                Track(
                    title=entry.get("title") or "Sin titulo",
                    webpage_url=webpage_url,
                    duration=entry.get("duration"),
                    uploader=entry.get("uploader") or entry.get("channel") or entry.get("artist"),
                    thumbnail=entry.get("thumbnail"),
                    source=self._detect_source(entry, source),
                    search_query=query,
                    requester_id=requester_id,
                    requester_name=requester_name,
                )
            )

        if not tracks:
            return []

        return tracks

    def _resolve_entry_webpage_url(self, entry: dict[str, Any], fallback_query: str) -> str:
        webpage_url = entry.get("webpage_url") or entry.get("original_url")
        if webpage_url:
            return webpage_url

        raw_url = entry.get("url")
        if not raw_url:
            return fallback_query

        raw_url_str = str(raw_url)
        if raw_url_str.startswith("http"):
            return raw_url_str

        ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
        if "youtube" in ie_key:
            return f"https://www.youtube.com/watch?v={raw_url_str}"

        return fallback_query

    def _prepare_stream_sync(self, track: Track) -> Track:
        try:
            info = self._extract_stream_info(track.webpage_url, track.source)
        except Exception as exc:  # pragma: no cover - runtime/network path
            raise self._translate_lookup_error(track.source, exc) from exc

        stream_url, stream_headers = self._extract_stream_candidate(info)
        if not stream_url:
            raise PlaybackError(f"No pude obtener el audio de **{track.title}**.")

        track.stream_url = stream_url
        track.stream_headers = stream_headers
        track.duration = info.get("duration") or track.duration
        track.thumbnail = info.get("thumbnail") or track.thumbnail
        track.uploader = info.get("uploader") or info.get("channel") or info.get("artist") or track.uploader
        track.title = info.get("title") or track.title
        track.webpage_url = info.get("webpage_url") or track.webpage_url
        track.source = self._detect_source(info, track.source)
        return track

    def _extract_stream_info(self, webpage_url: str, source: str) -> dict[str, Any]:
        if source != "youtube":
            stream_options = self._build_ydl_options(mode="stream", limit=1, source=source, direct_url=True)
            with yt_dlp.YoutubeDL(stream_options) as downloader:
                return downloader.extract_info(webpage_url, download=False)

        last_error: Exception | None = None
        for route_index, client_route in enumerate(self._youtube_stream_client_routes()):
            stream_options = self._build_ydl_options(
                mode="stream",
                limit=1,
                source=source,
                direct_url=True,
                youtube_retry=route_index > 0,
                youtube_player_clients=client_route,
            )
            route_label = ",".join(client_route)
            try:
                with yt_dlp.YoutubeDL(stream_options) as downloader:
                    info = downloader.extract_info(webpage_url, download=False)
                stream_url, _ = self._extract_stream_candidate(info)
                if stream_url:
                    self.logger.info("YouTube stream resuelto con cliente(s): %s", route_label)
                    return info
                last_error = PlaybackError("YouTube no entrego un stream reproducible.")
                self.logger.info("Ruta YouTube sin stream util: %s", route_label)
            except Exception as exc:
                last_error = exc
                translated = self._translate_lookup_error(source, exc)
                self.logger.info("Ruta YouTube fallida con cliente(s) %s: %s", route_label, translated)
                continue

        if last_error is not None:
            raise last_error
        raise PlaybackError("YouTube no entrego un stream reproducible.")

    def _build_ydl_options(
        self,
        mode: str,
        limit: int,
        source: str,
        direct_url: bool,
        playlist_like: bool = False,
        youtube_retry: bool = False,
        youtube_player_clients: list[str] | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": mode == "stream",
            "socket_timeout": 10 if mode == "stream" else 20,
            "geo_bypass": True,
            "force_ipv4": True,
            "extractor_retries": 1 if mode == "stream" else 2,
            "file_access_retries": 1 if mode == "stream" else 2,
        }

        if self.proxy_override is not None:
            options["proxy"] = self.proxy_override
        if source == "youtube" and self.youtube_cookiefile:
            options["cookiefile"] = self.youtube_cookiefile

        if source == "youtube":
            player_clients = (
                youtube_player_clients
                if youtube_player_clients is not None
                else
                self.settings.ytdlp_youtube_retry_player_clients
                if youtube_retry
                else self.settings.ytdlp_youtube_player_clients
            )
            extractor_args: dict[str, dict[str, list[str]]] = {
                "youtube": {
                    "player_client": player_clients,
                },
                "youtubepot-bgutilhttp": {
                    "base_url": [self.settings.ytdlp_bgutil_base_url],
                },
            }
            if self.settings.ytdlp_youtube_po_tokens:
                extractor_args["youtube"]["po_token"] = self.settings.ytdlp_youtube_po_tokens
            if self.settings.ytdlp_youtube_visitor_data:
                extractor_args["youtube"]["visitor_data"] = [self.settings.ytdlp_youtube_visitor_data]
                extractor_args["youtube"]["player_skip"] = ["webpage", "configs"]
            if self.settings.ytdlp_bgutil_server_home:
                extractor_args["youtubepot-bgutilscript"] = {
                    "server_home": [self.settings.ytdlp_bgutil_server_home],
                }

            options.update(
                {
                    "js_runtimes": {runtime.lower(): {} for runtime in self.settings.ytdlp_js_runtimes},
                    "remote_components": self.settings.ytdlp_remote_components,
                    "extractor_args": extractor_args,
                }
            )

        if mode == "metadata":
            if playlist_like and direct_url:
                options.update(
                    {
                        "playlistend": max(1, limit),
                        "ignoreerrors": True,
                    }
                )
            elif not direct_url:
                options.update(
                    {
                        "extract_flat": "in_playlist",
                        "playlistend": max(1, limit),
                    }
                )
        else:
            options.update(
                {
                    "format": "bestaudio[acodec!=none][protocol!=http_dash_segments]/bestaudio/best",
                }
            )

        return options

    def _youtube_stream_client_routes(self) -> list[list[str]]:
        raw_routes = self.settings.ytdlp_youtube_stream_routes or []
        routes: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        for raw_route in raw_routes:
            clients = [item.strip() for item in re.split(r"[+|]", raw_route) if item.strip()]
            if not clients:
                continue
            key = tuple(clients)
            if key in seen:
                continue
            seen.add(key)
            routes.append(clients)

        if not routes:
            routes.append(["web_safari"])
            routes.append(["mweb"])
        return routes

    def _build_target(self, query: str, source: str, limit: int) -> str:
        if is_url(query):
            return query
        if source == "youtube":
            return f"ytsearch{max(1, limit)}:{query}"
        raise PlaybackError(f"{format_source_label(source)} no soporta busqueda por texto en esta version.")

    def _extract_stream_candidate(self, info: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
        formats = info.get("formats") or []
        direct_url = info.get("url")
        direct_headers = self._normalize_headers(info.get("http_headers"))
        audio_only_formats = [
            item
            for item in formats
            if item.get("url")
            and item.get("acodec") not in (None, "none")
            and item.get("vcodec") == "none"
            and not self._is_fragmented_stream_protocol(item)
        ]
        selected = self._pick_best_format(
            audio_only_formats,
            key=lambda item: (
                self._audio_protocol_rank(item, prefer_hls=self._is_youtube_info(info)),
                item.get("abr") or 0,
                item.get("asr") or 0,
                1 if item.get("acodec") in {"opus", "vorbis"} else 0,
                1 if item.get("ext") in {"webm", "m4a"} else 0,
            ),
        )
        if selected:
            return selected.get("url"), self._normalize_headers(selected.get("http_headers") or info.get("http_headers"))

        if direct_url and (not self._is_youtube_info(info) or info.get("vcodec") == "none" or not formats):
            return direct_url, direct_headers

        if self._is_youtube_info(info):
            progressive_formats = [
                item
                for item in formats
                if item.get("url")
                and item.get("acodec") not in (None, "none")
                and item.get("vcodec") not in (None, "none")
                and item.get("ext") == "mp4"
                and not self._is_fragmented_stream_protocol(item)
                and str(item.get("protocol", "")).startswith(("http", "m3u8"))
            ]
            selected = self._pick_best_format(
                progressive_formats,
                key=lambda item: (
                    1 if str(item.get("protocol", "")).startswith("http") else 0,
                    item.get("quality") or 0,
                    item.get("height") or 0,
                ),
            )
            if selected:
                return selected.get("url"), self._normalize_headers(selected.get("http_headers") or info.get("http_headers"))

        fallback_audio_formats = [
            item
            for item in formats
            if item.get("url")
            and item.get("acodec") not in (None, "none")
            and not self._is_fragmented_stream_protocol(item)
        ]
        selected = self._pick_best_format(
            fallback_audio_formats,
            key=lambda item: (
                1 if item.get("vcodec") == "none" else 0,
                self._audio_protocol_rank(item, prefer_hls=self._is_youtube_info(info)),
                item.get("abr") or 0,
                item.get("asr") or 0,
                1 if item.get("acodec") in {"opus", "vorbis"} else 0,
                1 if item.get("ext") in {"webm", "m4a"} else 0,
            ),
        )
        if not selected:
            if direct_url:
                return direct_url, direct_headers
            return None, {}

        return selected.get("url"), self._normalize_headers(selected.get("http_headers") or info.get("http_headers"))

    def _translate_lookup_error(self, source: str, exc: Exception) -> PlaybackError:
        if isinstance(exc, PlaybackError):
            return exc

        message = str(exc)
        source_label = format_source_label(source)

        if source == "youtube" and ("Private video" in message or "This video is not available" in message or "Video unavailable" in message):
            return PlaybackError("YouTube no permite esa pista porque esta privada o ya no esta disponible.")
        if source == "youtube" and "Sign in to confirm you" in message:
            return PlaybackError("YouTube bloqueo temporalmente esta pista.")
        if "No formats" in message or "Requested format is not available" in message:
            return PlaybackError(f"{source_label} no entrego un stream reproducible.")

        return PlaybackError(f"{source_label} no pudo resolver esa solicitud.")

    def _resolve_spotify_queries(self, match: re.Match[str]) -> list[str]:
        if self.spotify_client is None:
            raise ConfigurationError(
                "Recibi una URL de Spotify, pero faltan SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET."
            )

        kind = match.group("kind") or match.group("kind_uri")
        spotify_id = match.group("id") or match.group("id_uri")
        if not kind or not spotify_id:
            raise UserInputError("La URL de Spotify no parece valida.")

        if kind == "track":
            item = self.spotify_client.track(spotify_id)
            return [self._spotify_track_to_query(item)]

        if kind == "album":
            album = self.spotify_client.album(spotify_id)
            tracks = album.get("tracks", {}).get("items", [])
            return [self._spotify_track_to_query(item) for item in tracks[: self.settings.spotify_playlist_limit]]

        if kind == "playlist":
            playlist_items = self.spotify_client.playlist_items(spotify_id, additional_types=("track",))
            queries: list[str] = []
            for item in playlist_items.get("items", []):
                track = item.get("track")
                if not track:
                    continue
                queries.append(self._spotify_track_to_query(track))
                if len(queries) >= self.settings.spotify_playlist_limit:
                    break
            return queries

        raise UserInputError("Ese tipo de recurso de Spotify todavia no esta soportado.")

    def _spotify_track_to_query(self, track: dict[str, Any]) -> str:
        artists = ", ".join(artist["name"] for artist in track.get("artists", []) if artist.get("name"))
        name = track.get("name") or "Unknown track"
        return f"{artists} - {name}"

    def _detect_source(self, info: dict[str, Any], requested_source: str) -> str:
        extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or "")

        if requested_source == "youtube" or "youtube" in extractor or YOUTUBE_URL_RE.match(webpage_url):
            return "youtube"
        if "spotify" in extractor or "spotify" in webpage_url:
            return "spotify"
        if requested_source != "auto":
            return requested_source
        return extractor or "yt-dlp"

    def _is_youtube_info(self, info: dict[str, Any]) -> bool:
        extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or "")
        return "youtube" in extractor or bool(YOUTUBE_URL_RE.match(webpage_url))

    def _pick_best_format(
        self,
        formats: list[dict[str, Any]],
        key: Any,
    ) -> dict[str, Any] | None:
        if not formats:
            return None
        formats.sort(key=key, reverse=True)
        return formats[0]

    def _is_fragmented_stream_protocol(self, format_info: dict[str, Any]) -> bool:
        protocol = str(format_info.get("protocol") or "").lower()
        return protocol == "http_dash_segments"

    def _audio_protocol_rank(self, format_info: dict[str, Any], *, prefer_hls: bool) -> int:
        protocol = str(format_info.get("protocol") or "").lower()
        if prefer_hls and protocol in {"m3u8", "m3u8_native"}:
            return 3
        if protocol.startswith(("https", "http")):
            return 2
        if protocol.startswith(("m3u8", "hls")):
            return 1
        return 0

    def _normalize_headers(self, headers: dict[str, Any] | None) -> dict[str, str]:
        if not headers:
            return {}
        normalized: dict[str, str] = {}
        for key, value in headers.items():
            if value is None:
                continue
            normalized[str(key)] = str(value)
        return normalized

    def _is_expandable_media_url(self, query: str) -> bool:
        if not is_url(query):
            return False

        lowered = query.lower()
        return "list=" in lowered or "/playlist" in lowered or "/playlists/" in lowered or "/sets/" in lowered

    def _should_skip_metadata_entry(self, entry: dict[str, Any], source: str) -> bool:
        if source != "youtube":
            return False

        lowered_title = str(entry.get("title") or "").strip().lower()
        if lowered_title in UNAVAILABLE_YOUTUBE_TITLES:
            return True

        return False

    def _should_retry_youtube_stream_resolution(self, message: str) -> bool:
        lowered = message.lower()
        return (
            "youtube bloqueo temporalmente" in lowered
            or "no entrego un stream reproducible" in lowered
            or "no pudo resolver esa solicitud" in lowered
            or "no pude obtener el audio" in lowered
        )
