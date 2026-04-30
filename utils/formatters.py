from __future__ import annotations

from math import ceil

import discord

from utils.audio_effects import describe_audio_effects
from utils.models import GuildMusicState, Track
from utils.source_router import format_source_label


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Desconocida"
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remainder:02d}"
    return f"{minutes}:{remainder:02d}"


def truncate(text: str | None, limit: int = 100) -> str:
    if not text:
        return "Desconocido"
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def progress_bar(current: int, total: int | None, length: int = 14) -> str:
    if not total or total <= 0:
        return "En vivo / sin duracion fija"
    ratio = max(0.0, min(current / total, 1.0))
    filled = min(length, ceil(length * ratio))
    return f"[{'=' * filled}{'-' * (length - filled)}]"


def build_success_embed(title: str, description: str, color: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def build_error_embed(message: str, color: int) -> discord.Embed:
    return discord.Embed(title="Error", description=message, color=discord.Color.red() if color == 0 else color)


def build_track_embed(
    track: Track,
    heading: str,
    color: int,
    voice_channel_name: str | None = None,
    state: GuildMusicState | None = None,
) -> discord.Embed:
    description_lines = [f"[{truncate(track.title, 180)}]({track.webpage_url})"]
    if state is not None and state.current is not None and state.current.id == track.id:
        elapsed = state.elapsed_seconds()
        description_lines.append(
            f"`{format_duration(elapsed)} / {format_duration(track.duration)}` `{progress_bar(elapsed, track.duration)}`"
        )
        description_lines.append(
            f"Vol {int(state.volume * 100)}% · Loop {state.repeat_mode.value} · Auto {'on' if state.autoplay_enabled else 'off'}"
        )

    embed = discord.Embed(
        title=heading,
        description="\n".join(description_lines),
        color=color,
    )
    embed.add_field(name="Duracion", value=format_duration(track.duration), inline=True)
    embed.add_field(name="Fuente", value=format_source_label(track.source), inline=True)
    embed.add_field(name="Autor", value=truncate(track.uploader or "Desconocido", 100), inline=True)
    requester_value = truncate(track.requester_name or "Anonimo", 100)
    if track.requester_id:
        requester_value = f"<@{track.requester_id}>"
    embed.add_field(name="Solicitado por", value=requester_value, inline=True)
    if voice_channel_name:
        embed.add_field(name="Conectado en", value=truncate(voice_channel_name, 100), inline=True)
    if state is not None:
        embed.add_field(name="Audio", value=describe_audio_effects(state), inline=False)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def build_now_playing_embed(state: GuildMusicState, color: int) -> discord.Embed:
    if state.current is None:
        return discord.Embed(title="Nada reproduciendose", description="La cola esta vacia.", color=color)

    track = state.current
    elapsed = state.elapsed_seconds()
    total = track.duration
    embed = discord.Embed(
        title="Ahora suena",
        description=f"[{truncate(track.title, 180)}]({track.webpage_url})",
        color=color,
    )
    embed.add_field(
        name="Progreso",
        value=f"`{progress_bar(elapsed, total)}`\n{format_duration(elapsed)} / {format_duration(total)}",
        inline=False,
    )
    embed.add_field(name="Fuente", value=format_source_label(track.source), inline=True)
    embed.add_field(name="Autor", value=truncate(track.uploader or "Desconocido", 100), inline=True)
    embed.add_field(name="Volumen", value=f"{int(state.volume * 100)}%", inline=True)
    embed.add_field(name="Loop", value=state.repeat_mode.value, inline=True)
    embed.add_field(name="Audio", value=describe_audio_effects(state), inline=True)
    embed.add_field(
        name="Conectado en",
        value=truncate(getattr(getattr(state.voice_client, "channel", None), "name", None) or "Desconocido", 100),
        inline=True,
    )
    embed.add_field(name="AutoPlay", value="on" if state.autoplay_enabled else "off", inline=True)
    if track.requester_name:
        embed.set_footer(text=f"Pedido por {track.requester_name}")
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def build_queue_embed(state: GuildMusicState, page: int, color: int) -> discord.Embed:
    per_page = 10
    page = max(page, 1)
    queue_items = list(state.queue)
    total_pages = max(1, ceil(len(queue_items) / per_page))
    page = min(page, total_pages)
    start = (page - 1) * per_page
    end = start + per_page

    embed = discord.Embed(title="Cola de reproduccion", color=color)
    if state.current:
        embed.description = f"**Ahora suena:** [{truncate(state.current.title, 90)}]({state.current.webpage_url})"
    else:
        embed.description = "No hay una cancion en curso."

    if not queue_items:
        embed.add_field(name="Siguiente", value="La cola esta vacia.", inline=False)
    else:
        lines = []
        for index, track in enumerate(queue_items[start:end], start=start + 1):
            lines.append(
                f"`{index:02d}` [{truncate(track.title, 70)}]({track.webpage_url}) "
                f"| {format_duration(track.duration)} | {format_source_label(track.source)} | "
                f"{truncate(track.requester_name or 'Anonimo', 30)}"
            )
        embed.add_field(name="Pendientes", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Pagina {min(page, total_pages)}/{total_pages} | Loop: {state.repeat_mode.value}")
    return embed


def split_message(content: str, limit: int = 1900) -> list[str]:
    if len(content) <= limit:
        return [content]
    chunks: list[str] = []
    current = []
    current_length = 0
    for line in content.splitlines():
        if current_length + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line) + 1
        else:
            current.append(line)
            current_length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
