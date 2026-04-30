from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.audio_effects import describe_audio_effects
from utils.formatters import build_success_embed
from utils.validators import validate_filter_preset, validate_pitch, validate_speed, validate_volume

FILTER_CHOICES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="bassboost", value="bassboost"),
    app_commands.Choice(name="clear", value="clear"),
    app_commands.Choice(name="radio", value="radio"),
    app_commands.Choice(name="nightcore", value="nightcore"),
    app_commands.Choice(name="vaporwave", value="vaporwave"),
]


class ControlsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="volume", description="Ajusta el volumen del bot entre 0 y 100.")
    @app_commands.describe(level="Nuevo volumen")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
        self.music.assert_control_access(interaction)
        volume = self.music.set_volume(interaction.guild_id, validate_volume(level))
        state = self.music.get_state(interaction.guild_id)
        if state.current:
            await self.music.refresh_active_panel(interaction.guild_id, heading="Audio actualizado")
        await interaction.response.send_message(
            embed=build_success_embed(
                "Volumen actualizado",
                f"El volumen quedo en **{volume}%**.\nPerfil actual: **{describe_audio_effects(state)}**.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="speed", description="Cambia la velocidad de reproduccion entre 0.5x y 2.0x.")
    @app_commands.describe(level="Nueva velocidad")
    async def speed(self, interaction: discord.Interaction, level: app_commands.Range[float, 0.5, 2.0]) -> None:
        self.music.assert_control_access(interaction)
        speed, restarted = await self.music.set_speed(interaction.guild_id, validate_speed(level))
        await interaction.response.send_message(
            embed=build_success_embed(
                "Velocidad actualizada",
                self._audio_feedback(
                    interaction.guild_id,
                    f"La velocidad base quedo en **{speed:.2f}x**.",
                    restarted,
                ),
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="pitch", description="Cambia el pitch de la reproduccion entre -12 y 12 semitonos.")
    @app_commands.describe(semitones="Cambio de pitch en semitonos")
    async def pitch(self, interaction: discord.Interaction, semitones: app_commands.Range[int, -12, 12]) -> None:
        self.music.assert_control_access(interaction)
        pitch, restarted = await self.music.set_pitch(interaction.guild_id, validate_pitch(semitones))
        await interaction.response.send_message(
            embed=build_success_embed(
                "Pitch actualizado",
                self._audio_feedback(
                    interaction.guild_id,
                    f"El pitch base quedo en **{pitch:+d} semitonos**.",
                    restarted,
                ),
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="filter", description="Aplica un preset de audio a la reproduccion actual o futura.")
    @app_commands.describe(preset="Preset de audio")
    @app_commands.choices(preset=FILTER_CHOICES)
    async def filter(self, interaction: discord.Interaction, preset: app_commands.Choice[str]) -> None:
        self.music.assert_control_access(interaction)
        selected, restarted = await self.music.set_filter(
            interaction.guild_id,
            validate_filter_preset(preset.value),
        )
        await interaction.response.send_message(
            embed=build_success_embed(
                "Preset actualizado",
                self._audio_feedback(
                    interaction.guild_id,
                    f"El preset activo ahora es **{selected.value}**.",
                    restarted,
                ),
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="effectsreset", description="Restaura velocidad, pitch y preset al estado normal.")
    async def effectsreset(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        restarted = await self.music.reset_audio_effects(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_success_embed(
                "Audio restaurado",
                self._audio_feedback(
                    interaction.guild_id,
                    "Volvi a **Normal** en velocidad, pitch y preset.",
                    restarted,
                ),
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="stop", description="Detiene la musica, limpia la cola y desconecta el bot.")
    async def stop(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        await self.music.stop(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_success_embed(
                "Reproduccion detenida",
                "Limpie la cola y me desconecte del canal de voz.",
                self.bot.settings.bot_color,
            )
        )

    def _audio_feedback(self, guild_id: int, headline: str, restarted: bool) -> str:
        state = self.music.get_state(guild_id)
        status = (
            "Reinicie la pista actual para aplicar el cambio ahora mismo."
            if restarted
            else "Lo deje guardado para la proxima reproduccion."
        )
        return f"{headline}\nPerfil actual: **{describe_audio_effects(state)}**.\n{status}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ControlsCog(bot))
