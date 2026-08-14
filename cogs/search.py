"""cogs/search.py — /search con selector desplegable + /lyrics (degradado)."""
from __future__ import annotations

from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.models import Track
from utils.ui import error_embed, success_embed, track_embed, truncate


class SearchSelect(discord.ui.Select):
    def __init__(self, cog: "SearchCog", tracks: list[Track]) -> None:
        self.cog = cog
        self.tracks = tracks
        options = [
            discord.SelectOption(
                label=f"{i}. {truncate(t.title, 80)}",
                value=str(i - 1),
                description=truncate(f"{t.uploader or 'Autor desconocido'}", 90),
            )
            for i, t in enumerate(tracks, start=1)
        ]
        super().__init__(
            placeholder="Selecciona una cancion",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        idx = int(self.values[0])
        if idx < 0 or idx >= len(self.tracks):
            await interaction.response.send_message(
                "Esa opcion ya no es valida.", ephemeral=True
            )
            return
        track = self.tracks[idx]
        try:
            accepted = await self.cog.music.enqueue_tracks(interaction, [track])
        except Exception as exc:
            # enqueue_tracks requiere voz; si el usuario salio del canal
            # entre el /search y el click, garantizamos un mensaje claro en
            # vez del "This interaction failed" generico de Discord.
            await interaction.response.send_message(
                embed=error_embed(str(exc) or "No pude agregar la cancion.", self.cog.bot.settings.bot_color),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=track_embed(accepted[0], "Agregada", self.cog.bot.settings.bot_color),
            ephemeral=True,
        )


class SearchResultsView(discord.ui.View):
    def __init__(self, cog: "SearchCog", tracks: list[Track]) -> None:
        super().__init__(timeout=180)
        self.add_item(SearchSelect(cog, tracks))


class SearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="search", description="Busca canciones y elige entre resultados.")
    @app_commands.describe(query="Termino de busqueda")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        tracks = await self.music.search(interaction, query, limit=5)
        if not tracks:
            await interaction.followup.send(
                embed=success_embed(
                    "Sin resultados",
                    "No encontre canciones para esa busqueda.",
                    self.bot.settings.bot_color,
                )
            )
            return
        lines = [
            f"`{i}` [{truncate(t.title, 80)}]({t.webpage_url}) - {truncate(t.uploader or '?', 36)}"
            for i, t in enumerate(tracks, start=1)
        ]
        embed = success_embed("Resultados", "\n".join(lines), self.bot.settings.bot_color)
        embed.set_footer(text="Usa el selector para agregar una cancion.")
        await interaction.followup.send(embed=embed, view=SearchResultsView(self, tracks))

    @app_commands.command(name="lyrics", description="Muestra la letra de la cancion actual.")
    async def lyrics(self, interaction: discord.Interaction) -> None:
        state = self.music.get_state(interaction.guild_id)
        if state.current is None:
            await interaction.response.send_message(
                embed=success_embed(
                    "Sin reproduccion",
                    "No hay una cancion sonando para buscar la letra.",
                    self.bot.settings.bot_color,
                ),
                ephemeral=True,
            )
            return
        # lyrics.ovh dejo de funcionar de forma estable en 2023. Mantenemos
        # el codigo como degradado: si responde, lo mostramos; si no, avisamos
        # al usuario y le damos el enlace a YouTube para leer los lyrics
        # oficiales en la descripcion del video.
        artist, title = self._guess_artist_and_title(state.current)
        await interaction.response.defer(thinking=True)
        lyrics_text: str | None = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as response:
                    if response.status == 200:
                        try:
                            payload = await response.json(content_type=None)
                            if isinstance(payload, dict):
                                lyrics_text = payload.get("lyrics")
                        except Exception:
                            lyrics_text = None
        except Exception:
            lyrics_text = None

        if not lyrics_text or not lyrics_text.strip():
            fallback = (
                f"No encontre letra sincronizada para **{truncate(state.current.title, 80)}**.\n\n"
                f"La API publica de letras (lyrics.ovh) ya no esta disponible "
                f"de forma estable.\n\n"
                f"Puedes consultar la letra oficial en la descripcion del video:\n"
                f"[Abrir en YouTube]({state.current.webpage_url})"
            )
            await interaction.followup.send(
                embed=success_embed(
                    "Letra no disponible",
                    fallback,
                    self.bot.settings.bot_color,
                )
            )
            return

        # Discord limita embeds a 4096 chars y descriptions a 4096.
        chunks = [lyrics_text[i:i + 3900] for i in range(0, len(lyrics_text), 3900)]
        first = success_embed(
            f"Letra de {truncate(state.current.title, 100)}",
            chunks[0],
            self.bot.settings.bot_color,
        )
        await interaction.followup.send(embed=first)
        for chunk in chunks[1:]:
            await interaction.followup.send(f"```\n{chunk}\n```")

    def _guess_artist_and_title(self, track: Track) -> tuple[str, str]:
        artist = track.uploader or "Unknown Artist"
        title = track.title
        if " - " in track.title:
            possible_artist, possible_title = track.title.split(" - ", 1)
            if not track.uploader or track.uploader.lower() in possible_artist.lower():
                artist = possible_artist
                title = possible_title
        return artist.strip(), title.strip()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SearchCog(bot))
