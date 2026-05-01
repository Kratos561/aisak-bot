from __future__ import annotations

from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.formatters import build_success_embed, build_track_embed, split_message, truncate
from utils.models import Track
from utils.query_autocomplete import build_query_choices
from utils.source_router import format_source_label


SOURCE_CHOICES = [
    app_commands.Choice(name="auto", value="auto"),
    app_commands.Choice(name="youtube", value="youtube"),
]


class SearchSelect(discord.ui.Select):
    def __init__(self, cog: "SearchCog", tracks: list[Track]) -> None:
        self.cog = cog
        self.tracks = tracks
        options = [
            discord.SelectOption(
                label=f"{index}. {truncate(track.title, 80)}",
                value=str(index - 1),
                description=truncate(
                    f"{format_source_label(track.source)} | {track.uploader or 'Autor desconocido'}",
                    90,
                ),
            )
            for index, track in enumerate(tracks, start=1)
        ]
        super().__init__(
            placeholder="Selecciona una cancion para agregar a la cola",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_track = self.tracks[int(self.values[0])]
        accepted = await self.cog.music.enqueue_tracks(interaction, [selected_track])
        await interaction.response.send_message(
            embed=build_track_embed(accepted[0], "Resultado agregado", self.cog.bot.settings.bot_color),
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

    @app_commands.command(name="search", description="Busca canciones en YouTube y deja elegir entre resultados.")
    @app_commands.describe(query="Termino de busqueda", source="Fuente")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def search(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        selected_source = source.value if source else "auto"
        tracks = await self.music.search(interaction, query, limit=5, source=selected_source)
        lines = [
            (
                f"`{index}` [{truncate(track.title, 80)}]({track.webpage_url}) - "
                f"{format_source_label(track.source)} - {truncate(track.uploader or 'Autor desconocido', 36)}"
            )
            for index, track in enumerate(tracks, start=1)
        ]
        embed = build_success_embed(
            "Resultados de busqueda",
            "\n".join(lines),
            self.bot.settings.bot_color,
        )
        embed.set_footer(text="Usa el selector para agregar una cancion a la cola.")
        await interaction.followup.send(embed=embed, view=SearchResultsView(self, tracks))

    @search.autocomplete("query")
    async def search_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        source = self._resolve_source_value(getattr(interaction.namespace, "source", None))
        return await build_query_choices(
            self.music.audio_service,
            current=current,
            requester_name=getattr(interaction.user, "display_name", "Autocomplete"),
            requester_id=getattr(interaction.user, "id", 0),
            source=source,
            limit=10,
        )

    @app_commands.command(name="lyrics", description="Busca la letra de la cancion actual.")
    async def lyrics(self, interaction: discord.Interaction) -> None:
        state = self.music.get_state(interaction.guild_id)
        if state.current is None:
            await interaction.response.send_message(
                embed=build_success_embed(
                    "Sin reproduccion",
                    "No hay una cancion sonando para buscar la letra.",
                    self.bot.settings.bot_color,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        artist, title = self._guess_artist_and_title(state.current)

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.bot.settings.lyrics_endpoint}/{quote(artist)}/{quote(title)}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                payload = await response.json(content_type=None)

        lyrics = payload.get("lyrics") if isinstance(payload, dict) else None
        if not lyrics:
            await interaction.followup.send(
                embed=build_success_embed(
                    "Letra no encontrada",
                    f"No encontre la letra de **{state.current.title}**.",
                    self.bot.settings.bot_color,
                )
            )
            return

        chunks = split_message(lyrics)
        first_embed = build_success_embed(
            f"Letra de {truncate(state.current.title, 120)}",
            chunks[0],
            self.bot.settings.bot_color,
        )
        await interaction.followup.send(embed=first_embed)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    def _guess_artist_and_title(self, track: Track) -> tuple[str, str]:
        artist = track.uploader or "Unknown Artist"
        title = track.title

        if " - " in track.title:
            possible_artist, possible_title = track.title.split(" - ", 1)
            if not track.uploader or track.uploader.lower() in possible_artist.lower():
                artist = possible_artist
                title = possible_title

        return artist.strip(), title.strip()

    def _resolve_source_value(self, raw_source) -> str:
        if hasattr(raw_source, "value"):
            return raw_source.value
        return raw_source or "auto"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SearchCog(bot))
