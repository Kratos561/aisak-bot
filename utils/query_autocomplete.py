from __future__ import annotations

import asyncio

from discord import app_commands

from utils.formatters import format_duration, truncate
from utils.validators import is_url


def _format_choice_name(track_title: str, uploader: str | None, duration: int | None) -> str:
    prefix = track_title
    if uploader and uploader.lower() not in track_title.lower():
        prefix = f"{uploader} - {track_title}"
    return truncate(f"{prefix} - {format_duration(duration)}", 100)


def _choice_value(url: str, fallback: str) -> str:
    if len(url) <= 100:
        return url
    return truncate(fallback, 100)


async def build_query_choices(
    audio_service,
    current: str,
    requester_name: str,
    requester_id: int,
    source: str,
    limit: int = 10,
) -> list[app_commands.Choice[str]]:
    normalized = current.strip()
    if len(normalized) < 2 or is_url(normalized):
        return []

    try:
        tracks = await asyncio.wait_for(
            audio_service.search_tracks(
                query=normalized,
                requester_name=requester_name,
                requester_id=requester_id,
                limit=limit,
                source="youtube" if source == "auto" else source,
            ),
            timeout=2.5,
        )
    except Exception:
        return []

    seen_values: set[str] = set()
    choices: list[app_commands.Choice[str]] = []
    for track in tracks:
        value = _choice_value(track.webpage_url, track.title)
        if value in seen_values:
            continue
        seen_values.add(value)
        choices.append(
            app_commands.Choice(
                name=_format_choice_name(track.title, track.uploader, track.duration),
                value=value,
            )
        )
        if len(choices) >= limit:
            break
    return choices
