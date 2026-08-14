"""cogs/music.py — Comandos de reproduccion."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.models import GuildMusicState, Track
from utils.ui import PlayerControlsView, success_embed, track_embed
from utils.validators import is_connected, is_url


class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    # ------------------------------------------------------------------ #
    # /play
    # ------------------------------------------------------------------ #
    @app_commands.command(name="play", description="Reproduce una cancion o URL de YouTube.")
    @app_commands.describe(query="Nombre de cancion, artista o URL de YouTube")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        # Vinculamos el panel al mensaje "thinking" ANTES de reproducir. Asi,
        # cuando start_next llame a _activate_panel, ya encuentra un
        # active_panel valido y edita este mismo mensaje con el embed del
        # track en reproduccion. Sin esto, el panel solo se actualizaba
        # tarde (race condition) y la primera cancion no mostraba controles.
        try:
            thinking_msg = await interaction.original_response()
            from utils.models import MessageRef
            state = self.music.get_state(interaction.guild_id)
            state.text_channel_id = interaction.channel_id
            state.active_panel = MessageRef(
                channel_id=thinking_msg.channel.id,
                message_id=thinking_msg.id,
            )
        except Exception:
            self.bot.logger.debug("No se pudo pre-vincular panel en /play.")

        tracks = await self.music.enqueue_query(interaction, query)
        state = self.music.get_state(interaction.guild_id)

        if len(tracks) == 1:
            embed = self._single_track_embed(tracks[0], state)
            view = PlayerControlsView(self.bot, interaction.guild_id) if state.current else None
        else:
            preview = "\n".join(f"- {t.title}" for t in tracks[:5])
            if len(tracks) > 5:
                preview += f"\n- ... y {len(tracks) - 5} mas"
            embed = success_embed(
                "Playlist agregada",
                f"Se agregaron **{len(tracks)}** canciones.\n\n{preview}",
                self.bot.settings.bot_color,
            )
            view = None

        try:
            message = await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            self.bot.logger.exception("Fallo respondiendo /play en guild=%s", interaction.guild_id)
            if not self.music.is_playing(interaction.guild_id):
                raise
            message = await interaction.followup.send(embed=embed, view=view, wait=True)

        # El panel ya fue pre-vinculado arriba. Si tras la edicion seguimos
        # reproduciendo, reaseignamos el message_id por si el mensaje cambio
        # (edit_original_response devuelve el mensaje actualizado).
        if state.current and message is not None:
            from utils.models import MessageRef
            state.active_panel = MessageRef(channel_id=message.channel.id, message_id=message.id)

    def _single_track_embed(self, track: Track, state: GuildMusicState) -> discord.Embed:
        current = state.current
        is_current = (
            current
            and current.id == track.id
            and state.player
            and is_connected(state.player)
        )
        if is_current:
            return track_embed(
                track, "Reproduccion iniciada", self.bot.settings.bot_color,
                voice_channel_name=getattr(getattr(state.player, "channel", None), "name", None),
                state=state,
            )
        return track_embed(
            track, "Cancion agregada a la cola", self.bot.settings.bot_color,
            voice_channel_name=getattr(getattr(state.player, "channel", None), "name", None),
            state=state,
        )

    @play.autocomplete("query")
    async def play_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(current)

    # ------------------------------------------------------------------ #
    # /pause
    # ------------------------------------------------------------------ #
    @app_commands.command(name="pause", description="Pausa la cancion actual.")
    async def pause(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        track = await self.music.pause(interaction.guild_id)
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.send_message(
            embed=track_embed(
                track, "Reproduccion pausada", self.bot.settings.bot_color,
                voice_channel_name=getattr(getattr(state.player, "channel", None), "name", None),
                state=state,
            )
        )

    # ------------------------------------------------------------------ #
    # /resume
    # ------------------------------------------------------------------ #
    @app_commands.command(name="resume", description="Reanuda la reproduccion pausada.")
    async def resume(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        track = await self.music.resume(interaction.guild_id)
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.send_message(
            embed=track_embed(
                track, "Reproduccion reanudada", self.bot.settings.bot_color,
                voice_channel_name=getattr(getattr(state.player, "channel", None), "name", None),
                state=state,
            )
        )

    # ------------------------------------------------------------------ #
    # /skip
    # ------------------------------------------------------------------ #
    @app_commands.command(name="skip", description="Salta una o varias canciones.")
    @app_commands.describe(count="Cuantas canciones saltar (default 1)")
    async def skip(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 25] = 1,
    ) -> None:
        self.music.assert_control_access(interaction)
        from utils.validators import validate_skip_count
        skipped = await self.music.skip(interaction.guild_id, validate_skip_count(count))
        description = f"Saltando **{skipped.title}**." if skipped else "Pase a la siguiente."
        await interaction.response.send_message(
            embed=success_embed("Skip ejecutado", description, self.bot.settings.bot_color)
        )

    # ------------------------------------------------------------------ #
    # /stop
    # ------------------------------------------------------------------ #
    @app_commands.command(name="stop", description="Detiene la musica y desconecta el bot.")
    async def stop(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        await self.music.stop(interaction.guild_id)
        await interaction.response.send_message(
            embed=success_embed(
                "Reproduccion detenida",
                "Limpie la cola y me desconecte.",
                self.bot.settings.bot_color,
            )
        )

    # ------------------------------------------------------------------ #
    # Autocomplete helper
    # ------------------------------------------------------------------ #
    async def _autocomplete(self, current: str) -> list[app_commands.Choice[str]]:
        from utils.query_autocomplete import build_query_choices
        normalized = current.strip()
        if len(normalized) < 2 or is_url(normalized):
            return []
        try:
            tracks = await build_query_choices(
                self.bot.lavalink,
                current=normalized,
                requester_name="autocomplete",
                requester_id=0,
                limit=10,
            )
        except Exception:
            return []
        return tracks


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
