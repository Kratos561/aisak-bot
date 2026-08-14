"""utils/ui.py — Embeds + vistas de Discord (botones interactivos).

Toda la presentacion visual del bot vive aqui: embeds de cola, now playing,
panel de controles con botones, etc.
"""
from __future__ import annotations

from math import ceil

import discord

from utils.models import FilterPreset, GuildMusicState, Track


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def truncate(text: str | None, limit: int = 100) -> str:
    if not text:
        return "Desconocido"
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds <= 0:
        return "Desconocida"
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remainder:02d}"
    return f"{minutes}:{remainder:02d}"


def progress_bar(current: int, total: int | None, length: int = 14) -> str:
    if not total or total <= 0:
        return "En vivo / sin duracion fija"
    ratio = max(0.0, min(current / total, 1.0))
    filled = min(length, ceil(length * ratio))
    return f"[{'=' * filled}{'-' * (length - filled)}]"


def describe_audio(state: GuildMusicState) -> str:
    parts: list[str] = []
    if state.playback_speed != 1.0:
        parts.append(f"{state.playback_speed:.2f}x")
    if state.pitch_semitones != 0:
        parts.append(f"{state.pitch_semitones:+d} semitonos")
    if state.filter_preset != FilterPreset.OFF:
        parts.append(state.filter_preset.value.capitalize())
    return " | ".join(parts) if parts else "Normal"


# ---------------------------------------------------------------------- #
# Embeds
# ---------------------------------------------------------------------- #
def success_embed(title: str, description: str, color: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def error_embed(message: str, color: int) -> discord.Embed:
    return discord.Embed(
        title="Error",
        description=message,
        color=discord.Color.red(),
    )


def track_embed(
    track: Track,
    heading: str,
    color: int,
    *,
    voice_channel_name: str | None = None,
    state: GuildMusicState | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=heading, color=color)
    embed.add_field(
        name="Cancion",
        value=truncate(track.title, 200),
        inline=False,
    )
    if track.uploader:
        embed.add_field(name="Canal", value=truncate(track.uploader, 100), inline=True)
    embed.add_field(name="Duracion", value=format_duration(track.duration), inline=True)
    if track.requester_name:
        embed.add_field(name="Pedido por", value=track.requester_name, inline=True)
    if voice_channel_name:
        embed.add_field(name="Canal de voz", value=voice_channel_name, inline=True)
    if state:
        embed.add_field(name="Audio", value=describe_audio(state), inline=True)
        repeat_label = {
            "off": "Desactivado",
            "one": "Una cancion",
            "all": "Toda la cola",
        }.get(state.repeat_mode.value, "Desactivado")
        embed.add_field(name="Repeticion", value=repeat_label, inline=True)
        if state.autoplay_enabled:
            embed.add_field(name="AutoPlay", value="Activado", inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    if track.webpage_url:
        embed.add_field(
            name="Enlace",
            value=f"[Abrir en YouTube]({track.webpage_url})",
            inline=False,
        )
    return embed


def now_playing_embed(state: GuildMusicState, color: int) -> discord.Embed:
    if state.current is None:
        return success_embed("Nada reproduciendose", "La cola esta vacia.", color)
    track = state.current
    elapsed = state.elapsed_seconds()
    bar = progress_bar(elapsed, track.duration)
    embed = discord.Embed(
        title="Reproduciendo ahora",
        description=f"**{truncate(track.title, 100)}**",
        color=color,
    )
    embed.add_field(
        name="Progreso",
        value=f"{bar} `{format_duration(elapsed)} / {format_duration(track.duration)}`",
        inline=False,
    )
    if track.uploader:
        embed.add_field(name="Canal", value=truncate(track.uploader, 80), inline=True)
    embed.add_field(name="Pedido por", value=track.requester_name or "?", inline=True)
    embed.add_field(name="Audio", value=describe_audio(state), inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def queue_embed(state: GuildMusicState, page: int, color: int) -> discord.Embed:
    items = list(state.queue)
    if not items:
        return success_embed("Cola vacia", "No hay canciones en espera.", color)
    per_page = 10
    pages_total = max(1, ceil(len(items) / per_page))
    page = max(1, min(page, pages_total))
    start = (page - 1) * per_page
    end = start + per_page
    chunk = items[start:end]
    lines = []
    for idx, track in enumerate(chunk, start=start + 1):
        title = truncate(track.title, 60)
        dur = format_duration(track.duration)
        lines.append(f"`{idx:>2}` **{title}** · {dur} · {track.requester_name or '?'}")
    embed = discord.Embed(
        title="Cola de reproduccion",
        description="\n".join(lines),
        color=color,
    )
    embed.set_footer(text=f"Pagina {page}/{pages_total} · {len(items)} canciones en cola")
    return embed


# ---------------------------------------------------------------------- #
# Player controls (panel interactivo)
# ---------------------------------------------------------------------- #
class PlayerControlsView(discord.ui.View):
    """Panel de botones persistentes para el mensaje now-playing."""
    def __init__(self, bot, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.button(label="Pausa", style=discord.ButtonStyle.primary, emoji="⏸️", custom_id="aisak:pause")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._delegate(interaction, "pause")

    @discord.ui.button(label="Reanudar", style=discord.ButtonStyle.success, emoji="▶️", custom_id="aisak:resume")
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._delegate(interaction, "resume")

    @discord.ui.button(label="Saltar", style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="aisak:skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._delegate(interaction, "skip")

    @discord.ui.button(label="Detener", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="aisak:stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._delegate(interaction, "stop")

    @discord.ui.button(label="AutoPlay", style=discord.ButtonStyle.secondary, emoji="♾️", custom_id="aisak:autoplay")
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._delegate(interaction, "autoplay")

    @discord.ui.button(label="Cola", style=discord.ButtonStyle.secondary, emoji="📜", custom_id="aisak:queue")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._delegate(interaction, "queue")

    async def _delegate(self, interaction: discord.Interaction, action: str) -> None:
        music = self.bot.music
        # guild_id se toma SIEMPRE del interaction, no del atributo de la
        # vista. La vista persistente registrada en setup_hook se crea con
        # guild_id=0 (placeholder); si usaramos self.guild_id ahi, ningun
        # boton de panel persistente funcionaria tras un reinicio del bot.
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                embed=error_embed("Esto solo funciona en un servidor.", self.bot.settings.bot_color),
                ephemeral=True,
            )
            return
        try:
            music.assert_control_access(interaction)
        except Exception as exc:
            await interaction.response.send_message(
                embed=error_embed(str(exc), self.bot.settings.bot_color),
                ephemeral=True,
            )
            return
        if action == "pause":
            await music.pause(guild_id)
            msg = "Reproduccion pausada."
        elif action == "resume":
            await music.resume(guild_id)
            msg = "Reproduccion reanudada."
        elif action == "skip":
            await music.skip(guild_id)
            msg = "Cancion saltada."
        elif action == "stop":
            await music.stop(guild_id)
            msg = "Reproduccion detenida."
        elif action == "autoplay":
            enabled = music.toggle_autoplay(guild_id)
            msg = f"AutoPlay {'activado' if enabled else 'desactivado'}."
        elif action == "queue":
            state = music.get_state(guild_id)
            await interaction.response.send_message(
                embed=queue_embed(state, 1, self.bot.settings.bot_color),
                ephemeral=True,
            )
            return
        else:
            msg = "Accion desconocida."
        await interaction.response.send_message(msg, ephemeral=True)
        await music.refresh_panel(guild_id)
