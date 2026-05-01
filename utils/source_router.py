from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from utils.errors import UserInputError
from utils.validators import is_url

YOUTUBE_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com)/", re.IGNORECASE)
SOUNDCLOUD_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:soundcloud\.com|on\.soundcloud\.com)/", re.IGNORECASE)
MIXCLOUD_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?mixcloud\.com/", re.IGNORECASE)

RARE_QUERY_TERMS = (
    "mix",
    "remix",
    "edit",
    "cover",
    "slowed",
    "reverb",
    "set",
    "live",
    "instrumental",
    "session",
    "bootleg",
    "vip",
)

SOURCE_LABELS = {
    "auto": "YouTube",
    "youtube": "YouTube",
    "soundcloud": "SoundCloud",
    "mixcloud": "Mixcloud",
    "spotify": "Spotify",
    "yt-dlp": "yt-dlp",
}


class QueryKind(str, Enum):
    URL_YOUTUBE = "url_youtube"
    URL_SOUNDCLOUD = "url_soundcloud"
    URL_MIXCLOUD = "url_mixcloud"
    URL_OTHER = "url_other"
    FREE_QUERY = "free_query"


@dataclass(slots=True)
class QueryPlan:
    query: str
    requested_source: str
    kind: QueryKind
    detected_source: str | None
    intent: str
    playback_sources: list[str]
    search_sources: list[str]


def format_source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.title())


def detect_source_from_url(query: str) -> str | None:
    if YOUTUBE_URL_RE.match(query):
        return "youtube"
    if SOUNDCLOUD_URL_RE.match(query):
        return "soundcloud"
    if MIXCLOUD_URL_RE.match(query):
        return "mixcloud"
    return None


def classify_query(query: str) -> QueryKind:
    detected_source = detect_source_from_url(query)
    if detected_source == "youtube":
        return QueryKind.URL_YOUTUBE
    if detected_source == "soundcloud":
        return QueryKind.URL_SOUNDCLOUD
    if detected_source == "mixcloud":
        return QueryKind.URL_MIXCLOUD
    if is_url(query):
        return QueryKind.URL_OTHER
    return QueryKind.FREE_QUERY


def infer_query_intent(query: str) -> str:
    lowered = query.lower()
    return "rare_or_mix" if any(term in lowered for term in RARE_QUERY_TERMS) else "general"


def build_query_plan(query: str, requested_source: str) -> QueryPlan:
    requested_source = (requested_source or "auto").lower()
    detected_source = detect_source_from_url(query)
    kind = classify_query(query)
    intent = infer_query_intent(query)

    if requested_source in {"soundcloud", "mixcloud"}:
        raise UserInputError("AISAK ahora usa solo YouTube. Usa `/youtube`, `/play` o un enlace de YouTube.")

    if requested_source not in {"auto", "youtube"}:
        raise UserInputError(f"La fuente `{requested_source}` no esta soportada.")

    if detected_source == "youtube":
        return QueryPlan(query, requested_source, kind, detected_source, intent, ["youtube"], ["youtube"])

    if detected_source in {"soundcloud", "mixcloud"}:
        raise UserInputError("Ese enlace no es de YouTube. AISAK ahora reproduce solamente desde YouTube.")

    if kind == QueryKind.URL_OTHER:
        raise UserInputError("Ese enlace no pertenece a YouTube. Usa un enlace de YouTube o busca por nombre.")

    return QueryPlan(query, requested_source, kind, detected_source, intent, ["youtube"], ["youtube"])
