"""utils/query_autocomplete.py — Genera Choice[] para autocomplete de /play."""
from __future__ import annotations

import asyncio

from discord import app_commands

from utils.ui import format_duration, truncate
from utils.validators import is_url


def _format_choice_name(title: str, uploader: str | None, duration: int | None) -> str:
    prefix = title
    if uploader and uploader.lower() not in title.lower():
        prefix = f"{uploader} - {title}"
    return truncate(f"{prefix} - {format_duration(duration)}", 100)


def _choice_value(url: str, fallback: str) -> str:
    if len(url) <= 100:
        return url
    return truncate(fallback, 100)


async def build_query_choices(
    track_source,
    current: str,
    requester_name: str,
    requester_id: int,
    limit: int = 10,
) -> list[app_commands.Choice[str]]:
    normalized = current.strip()
    if len(normalized) < 2 or is_url(normalized):
        return []
    try:
        tracks = await asyncio.wait_for(
            track_source.search_tracks(
                query=normalized,
                requester_name=requester_name,
                requester_id=requester_id,
                limit=limit,
            ),
            timeout=2.5,
        )
    except Exception:
        return []
    seen: set[str] = set()
    choices: list[app_commands.Choice[str]] = []
    for t in tracks:
        value = _choice_value(t.webpage_url, t.title)
        if value in seen:
            continue
        seen.add(value)
        choices.append(app_commands.Choice(
            name=_format_choice_name(t.title, t.uploader, t.duration),
            value=value,
        ))
        if len(choices) >= limit:
            break
    return choices
