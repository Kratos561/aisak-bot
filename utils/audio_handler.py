from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any

import yt_dlp

from config import Settings
from utils.errors import ConfigurationError, PlaybackError, UserInputError
from utils.models import Track
from utils.source_router import (
    MIXCLOUD_URL_RE,
    SOUNDCLOUD_URL_RE,
    YOUTUBE_URL_RE,
    QueryPlan,
    build_query_plan,
    format_source_label,
)
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
            spotify_queries = await asyncio.to_thread(self._resolve_spotify_queries, match)
            if not spotify_queries:
                raise PlaybackError("No pude resolver esa URL de Spotify a canciones reproducibles.")

            tracks: list[Track] = []
            spotify_source = source if source in {"soundcloud", "youtube"} else "auto"
            for spotify_query in spotify_queries[:limit]:
                tracks.extend(
                    await asyncio.to_thread(
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
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(
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
        try:
            return await asyncio.to_thread(self._prepare_stream_sync, track)
        except PlaybackError as exc:
            fallback = await self._prepare_alternative_stream(track, exc)
            if fallback is not None:
                return fallback
            raise

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
        stream_options = self._build_ydl_options(mode="stream", limit=1, source=track.source, direct_url=True)

        try:
            with yt_dlp.YoutubeDL(stream_options) as downloader:
                info = downloader.extract_info(track.webpage_url, download=False)
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

    def _build_ydl_options(
        self,
        mode: str,
        limit: int,
        source: str,
        direct_url: bool,
        playlist_like: bool = False,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": mode == "stream",
            "socket_timeout": 20,
            "geo_bypass": True,
        }

        if source == "youtube":
            extractor_args: dict[str, dict[str, list[str]]] = {
                "youtube": {
                    "player_client": self.settings.ytdlp_youtube_player_clients,
                },
                "youtubepot-bgutilhttp": {
                    "base_url": [self.settings.ytdlp_bgutil_base_url],
                },
            }
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

    def _build_target(self, query: str, source: str, limit: int) -> str:
        if is_url(query):
            return query
        if source == "soundcloud":
            return f"scsearch{max(1, limit)}:{query}"
        if source == "youtube":
            return f"ytsearch{max(1, limit)}:{query}"
        raise PlaybackError(f"{format_source_label(source)} no soporta busqueda por texto en esta version.")

    def _extract_stream_candidate(self, info: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
        formats = info.get("formats") or []
        if self._is_youtube_info(info):
            progressive_formats = [
                item
                for item in formats
                if item.get("url")
                and item.get("acodec") not in (None, "none")
                and item.get("vcodec") not in (None, "none")
                and item.get("ext") == "mp4"
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

        direct_url = info.get("url")
        if direct_url:
            return direct_url, self._normalize_headers(info.get("http_headers"))

        audio_formats = [item for item in formats if item.get("acodec") not in (None, "none") and item.get("url")]
        selected = self._pick_best_format(
            audio_formats,
            key=lambda item: (
                1 if item.get("vcodec") == "none" else 0,
                1 if str(item.get("protocol", "")).startswith("http") else 0,
                1 if item.get("ext") == "m4a" else 0,
                item.get("abr") or 0,
                item.get("asr") or 0,
            ),
        )
        if not selected:
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
        if source == "soundcloud" and ("Unauthorized" in message or "401" in message):
            return PlaybackError("SoundCloud rechazo la solicitud para esa pista.")
        if source == "mixcloud" and "Track unavailable in your country" in message:
            return PlaybackError("Mixcloud no permite reproducir esa pista en esta region.")
        if source == "mixcloud" and "Track not found" in message:
            return PlaybackError("No pude abrir ese enlace de Mixcloud.")
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
        if requested_source == "soundcloud" or "soundcloud" in extractor or SOUNDCLOUD_URL_RE.match(webpage_url):
            return "soundcloud"
        if requested_source == "mixcloud" or "mixcloud" in extractor or MIXCLOUD_URL_RE.match(webpage_url):
            return "mixcloud"
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

    async def _prepare_alternative_stream(self, track: Track, error: PlaybackError) -> Track | None:
        if track.source != "youtube":
            return None
        if not self._should_try_alternative_source(str(error)):
            return None

        requester_name = track.requester_name or "Fallback"
        requester_id = track.requester_id or 0

        for query in self._build_alternative_queries(track):
            try:
                candidates = await self.search_tracks(
                    query=query,
                    requester_name=requester_name,
                    requester_id=requester_id,
                    limit=3,
                    source="soundcloud",
                )
            except PlaybackError:
                continue

            for candidate in candidates:
                if candidate.webpage_url == track.webpage_url:
                    continue
                if self._fallback_match_score(track.title, candidate.title) < 0.5:
                    continue
                candidate.requester_name = requester_name
                candidate.requester_id = requester_id
                candidate.search_query = track.search_query or query
                try:
                    prepared = await asyncio.to_thread(self._prepare_stream_sync, candidate)
                except PlaybackError:
                    continue

                self.logger.info(
                    "Fallback de YouTube a SoundCloud para '%s' usando query '%s' -> '%s'",
                    track.title,
                    query,
                    prepared.title,
                )
                return prepared

        return None

    def _should_try_alternative_source(self, message: str) -> bool:
        lowered = message.lower()
        return (
            "youtube bloqueo temporalmente" in lowered
            or "youtube no permite esa pista" in lowered
            or "youtube no entrego un stream reproducible" in lowered
            or "no pude obtener el audio" in lowered
        )

    def _build_alternative_queries(self, track: Track) -> list[str]:
        candidates: list[str] = []

        def add(value: str | None) -> None:
            if not value:
                return
            cleaned = self._clean_fallback_query(value)
            if len(cleaned) < 3:
                return
            if cleaned not in candidates:
                candidates.append(cleaned)

        title = track.title or ""
        add(title)
        add(BRACKETED_SEGMENT_RE.sub(" ", title))

        title_without_brackets = BRACKETED_SEGMENT_RE.sub(" ", title)
        split_hyphen = [part.strip() for part in re.split(r"\s+-\s+|-", title_without_brackets) if part.strip()]
        if split_hyphen:
            add(split_hyphen[0])
            add(" ".join(split_hyphen[:2]))

        if track.uploader:
            add(f"{track.uploader} {title_without_brackets}")

        if track.search_query and not is_url(track.search_query):
            add(track.search_query)

        return candidates

    def _clean_fallback_query(self, value: str) -> str:
        cleaned = BRACKETED_SEGMENT_RE.sub(" ", value)
        cleaned = NON_WORD_QUERY_RE.sub(" ", cleaned)
        return " ".join(cleaned.split())

    def _fallback_match_score(self, original: str, candidate: str) -> float:
        original_tokens = self._match_tokens(original)
        candidate_tokens = self._match_tokens(candidate)
        if not original_tokens or not candidate_tokens:
            return 0.0

        overlap = len(set(original_tokens) & set(candidate_tokens)) / max(len(set(original_tokens)), 1)
        sequence = difflib.SequenceMatcher(
            None,
            " ".join(original_tokens),
            " ".join(candidate_tokens),
        ).ratio()
        return (overlap * 0.7) + (sequence * 0.3)

    def _match_tokens(self, value: str) -> list[str]:
        cleaned = self._clean_fallback_query(value).lower()
        return [token for token in cleaned.split() if token and token not in FALLBACK_STOP_TOKENS]
