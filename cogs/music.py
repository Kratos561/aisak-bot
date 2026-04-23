from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.formatters import build_now_playing_embed, build_success_embed, build_track_embed
from utils.models import GuildMusicState, Track
from utils.player_controls import PlayerControlsView
from utils.query_autocomplete import build_query_choices
from utils.validators import validate_skip_count


SOURCE_CHOICES = [
    app_commands.Choice(name="auto", value="auto"),
    app_commands.Choice(name="soundcloud", value="soundcloud"),
    app_commands.Choice(name="mixcloud", value="mixcloud"),
    app_commands.Choice(name="youtube", value="youtube"),
]


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="play", description="Busca y reproduce una cancion o URL.")
    @app_commands.describe(query="Nombre de cancion, artista, URL o enlace", source="Fuente preferida")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._play_impl(interaction, query, source.value if source else "auto")

    @app_commands.command(name="playlist", description="Agrega una playlist a la cola (mantiene el orden).")
    @app_commands.describe(url="URL de la playlist (por ejemplo YouTube)")
    async def playlist(self, interaction: discord.Interaction, url: str) -> None:
        await self._play_impl(interaction, url, "auto")

    @play.autocomplete("query")
    async def play_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_query(interaction, current)

    async def _play_impl(self, interaction: discord.Interaction, query: str, source: str) -> None:
        await interaction.response.defer(thinking=True)
        tracks = await self.music.enqueue_query(interaction, query, source=source)
        state = self.music.get_state(interaction.guild_id)
        heading = None
        active_track = None

        if len(tracks) == 1:
            embed = self._build_single_track_response(tracks[0], state)
            active_track, heading = self._resolve_active_panel_state(tracks[0], state)
            view = PlayerControlsView(self.bot, interaction.guild_id) if active_track else None
        else:
            preview = "\n".join(f"- {track.title}" for track in tracks[:5])
            if len(tracks) > 5:
                preview += f"\n- ... y {len(tracks) - 5} mas"
            embed = build_success_embed(
                "Playlist agregada",
                f"Se agregaron **{len(tracks)}** canciones a la cola.\n\n{preview}",
                self.bot.settings.bot_color,
            )
            view = None

        message = await interaction.followup.send(embed=embed, view=view, wait=True)
        if len(tracks) == 1 and message is not None:
            await self.music.register_track_message(
                interaction.guild_id,
                tracks[0],
                channel_id=message.channel.id,
                message_id=message.id,
                activate=bool(active_track),
                heading=heading or "Reproduccion iniciada",
            )

    def _build_single_track_response(self, track: Track, state: GuildMusicState) -> discord.Embed:
        current = state.current
        started_this_track = bool(
            current
            and current.webpage_url == track.webpage_url
            and state.voice_client
            and state.voice_client.is_connected()
        )
        queued_this_track = any(item.webpage_url == track.webpage_url for item in state.queue)

        if started_this_track:
            return build_track_embed(
                track,
                "Reproduccion iniciada",
                self.bot.settings.bot_color,
                voice_channel_name=getattr(getattr(state.voice_client, "channel", None), "name", None),
            )
        if queued_this_track:
            return build_track_embed(
                track,
                "Cancion agregada a la cola",
                self.bot.settings.bot_color,
                voice_channel_name=getattr(getattr(state.voice_client, "channel", None), "name", None),
            )
        return build_success_embed(
            "Solicitud procesada",
            (
                f"Intente preparar **{track.title}**, pero no quedo ni sonando ni en cola.\n"
                "Revisa el mensaje de estado que envie en el canal para ver la causa exacta."
            ),
            self.bot.settings.bot_color,
        )

    @app_commands.command(name="soundcloud", description="Busca y reproduce usando SoundCloud como fuente principal.")
    @app_commands.describe(query="Nombre de la cancion o URL de SoundCloud")
    async def soundcloud(self, interaction: discord.Interaction, query: str) -> None:
        await self._play_impl(interaction, query, "soundcloud")

    @soundcloud.autocomplete("query")
    async def soundcloud_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_query(interaction, current, forced_source="soundcloud")

    @app_commands.command(name="mixcloud", description="Reproduce un enlace directo de Mixcloud.")
    @app_commands.describe(url="Enlace directo de Mixcloud")
    async def mixcloud(self, interaction: discord.Interaction, url: str) -> None:
        await self._play_impl(interaction, url, "mixcloud")

    @app_commands.command(name="youtube", description="Busca y reproduce usando YouTube como fuente principal.")
    @app_commands.describe(query="Nombre de la cancion o URL de YouTube")
    async def youtube(self, interaction: discord.Interaction, query: str) -> None:
        await self._play_impl(interaction, query, "youtube")

    @youtube.autocomplete("query")
    async def youtube_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete_query(interaction, current, forced_source="youtube")

    @app_commands.command(name="pause", description="Pausa la reproduccion actual.")
    async def pause(self, interaction: discord.Interaction) -> None:
        track = await self.music.pause(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_track_embed(track, "Reproduccion pausada", self.bot.settings.bot_color)
        )

    @app_commands.command(name="resume", description="Reanuda la reproduccion pausada.")
    async def resume(self, interaction: discord.Interaction) -> None:
        track = await self.music.resume(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_track_embed(track, "Reproduccion reanudada", self.bot.settings.bot_color)
        )

    @app_commands.command(name="skip", description="Salta una o varias canciones.")
    @app_commands.describe(count="Cantidad de canciones a saltar")
    async def skip(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 25] = 1) -> None:
        skipped = await self.music.skip(interaction.guild_id, validate_skip_count(count))
        description = f"Saltando **{skipped.title}**." if skipped else "Pase a la siguiente cancion."
        await interaction.response.send_message(
            embed=build_success_embed("Skip ejecutado", description, self.bot.settings.bot_color)
        )

    @app_commands.command(name="nowplaying", description="Muestra la cancion actual con su progreso.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_now_playing_embed(state, self.bot.settings.bot_color),
            view=PlayerControlsView(self.bot, interaction.guild_id) if state.current else None,
        )
        if state.current:
            message = await interaction.original_response()
            await self.music.register_track_message(
                interaction.guild_id,
                state.current,
                channel_id=message.channel.id,
                message_id=message.id,
                activate=True,
                heading="Reproduccion iniciada",
            )

    async def _autocomplete_query(
        self,
        interaction: discord.Interaction,
        current: str,
        forced_source: str | None = None,
    ) -> list[app_commands.Choice[str]]:
        source = forced_source or self._resolve_source_value(getattr(interaction.namespace, "source", None))
        return await build_query_choices(
            self.music.audio_service,
            current=current,
            requester_name=getattr(interaction.user, "display_name", "Autocomplete"),
            requester_id=getattr(interaction.user, "id", 0),
            source=source,
            limit=10,
        )

    def _resolve_source_value(self, raw_source) -> str:
        if hasattr(raw_source, "value"):
            return raw_source.value
        return raw_source or "auto"

    def _resolve_active_panel_state(self, track: Track, state: GuildMusicState) -> tuple[Track | None, str | None]:
        current = state.current
        if (
            current
            and current.webpage_url == track.webpage_url
            and state.voice_client
            and state.voice_client.is_connected()
        ):
            return current, "Reproduccion iniciada"
        return None, None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
