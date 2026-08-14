"""cogs/controls.py — Efectos de audio: volume, speed, pitch, filter, effectsreset."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.ui import describe_audio, success_embed
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

    @app_commands.command(name="volume", description="Cambia el volumen (0-100).")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
        self.music.assert_control_access(interaction)
        v = await self.music.set_volume(interaction.guild_id, validate_volume(level))
        await self._reply(interaction, f"Volumen: **{v}%**.")

    @app_commands.command(name="speed", description="Cambia la velocidad (0.5x-2.0x).")
    async def speed(self, interaction: discord.Interaction, level: app_commands.Range[float, 0.5, 2.0]) -> None:
        self.music.assert_control_access(interaction)
        s, _ = await self.music.set_speed(interaction.guild_id, validate_speed(level))
        await self._reply(interaction, f"Velocidad: **{s:.2f}x**.")

    @app_commands.command(name="pitch", description="Cambia el pitch (-12 a +12 semitonos).")
    async def pitch(self, interaction: discord.Interaction, semitones: app_commands.Range[int, -12, 12]) -> None:
        self.music.assert_control_access(interaction)
        p, _ = await self.music.set_pitch(interaction.guild_id, validate_pitch(semitones))
        await self._reply(interaction, f"Pitch: **{p:+d} semitonos**.")

    @app_commands.command(name="filter", description="Aplica un preset de audio.")
    @app_commands.choices(preset=FILTER_CHOICES)
    async def filter(self, interaction: discord.Interaction, preset: app_commands.Choice[str]) -> None:
        self.music.assert_control_access(interaction)
        f, _ = await self.music.set_filter(interaction.guild_id, validate_filter_preset(preset.value))
        await self._reply(interaction, f"Preset: **{f.value}**.")

    @app_commands.command(name="effectsreset", description="Restaura velocidad, pitch y preset a normal.")
    async def effectsreset(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        await self.music.reset_effects(interaction.guild_id)
        await self._reply(interaction, "Audio restaurado a **Normal**.")

    async def _reply(self, interaction: discord.Interaction, headline: str) -> None:
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.send_message(
            embed=success_embed(
                "Audio actualizado",
                f"{headline}\nPerfil: **{describe_audio(state)}**.",
                self.bot.settings.bot_color,
            )
        )
        await self.music.refresh_panel(interaction.guild_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ControlsCog(bot))
