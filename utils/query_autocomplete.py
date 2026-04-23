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
        if source == "auto":
            tracks = await _search_auto_choices(
                audio_service,
                normalized,
                requester_name,
                requester_id,
                limit,
            )
        else:
            tracks = await asyncio.wait_for(
                audio_service.search_tracks(
                    query=normalized,
                    requester_name=requester_name,
                    requester_id=requester_id,
                    limit=limit,
                    source=source,
                ),
                timeout=2.8,
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


async def _search_auto_choices(
    audio_service,
    normalized: str,
    requester_name: str,
    requester_id: int,
    limit: int,
):
    async def run_source(source_name: str):
        try:
            return await audio_service.search_tracks(
                query=normalized,
                requester_name=requester_name,
                requester_id=requester_id,
                limit=limit,
                source=source_name,
            )
        except Exception:
            return []

    soundcloud_task = asyncio.create_task(run_source("soundcloud"))
    youtube_task = asyncio.create_task(run_source("youtube"))
    done, pending = await asyncio.wait({soundcloud_task, youtube_task}, timeout=2.6)

    for task in pending:
        task.cancel()

    results_by_task = {task: task.result() for task in done if not task.cancelled()}
    soundcloud_tracks = results_by_task.get(soundcloud_task, [])
    youtube_tracks = results_by_task.get(youtube_task, [])
    return (soundcloud_tracks + youtube_tracks)[:limit]
