from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from utils.errors import UserInputError
from utils.validators import is_url

YOUTUBE_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be|music\.youtube\.com)/", re.IGNORECASE)

SOURCE_LABELS = {
    "auto": "YouTube",
    "youtube": "YouTube",
    "spotify": "Spotify",
    "yt-dlp": "yt-dlp",
}


class QueryKind(str, Enum):
    URL_YOUTUBE = "url_youtube"
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
    return None


def classify_query(query: str) -> QueryKind:
    detected_source = detect_source_from_url(query)
    if detected_source == "youtube":
        return QueryKind.URL_YOUTUBE
    if is_url(query):
        return QueryKind.URL_OTHER
    return QueryKind.FREE_QUERY


def infer_query_intent(query: str) -> str:
    return "youtube_only"


def build_query_plan(query: str, requested_source: str) -> QueryPlan:
    requested_source = (requested_source or "auto").lower()
    detected_source = detect_source_from_url(query)
    kind = classify_query(query)
    intent = infer_query_intent(query)

    if requested_source not in {"auto", "youtube"}:
        raise UserInputError("AISAK ahora usa solo YouTube. Usa `/youtube`, `/play` o un enlace de YouTube.")

    if detected_source == "youtube":
        return QueryPlan(query, requested_source, kind, detected_source, intent, ["youtube"], ["youtube"])

    if kind == QueryKind.URL_OTHER:
        raise UserInputError("Ese enlace no pertenece a YouTube. Usa un enlace de YouTube o busca por nombre.")

    return QueryPlan(query, requested_source, kind, detected_source, intent, ["youtube"], ["youtube"])
