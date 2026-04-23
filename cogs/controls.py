from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.formatters import build_success_embed
from utils.validators import validate_volume


class ControlsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="volume", description="Ajusta el volumen del bot entre 0 y 100.")
    @app_commands.describe(level="Nuevo volumen")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
        volume = self.music.set_volume(interaction.guild_id, validate_volume(level))
        await interaction.response.send_message(
            embed=build_success_embed(
                "Volumen actualizado",
                f"El volumen quedo en **{volume}%**.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="stop", description="Detiene la musica, limpia la cola y desconecta el bot.")
    async def stop(self, interaction: discord.Interaction) -> None:
        await self.music.stop(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_success_embed(
                "Reproduccion detenida",
                "Limpie la cola y me desconecte del canal de voz.",
                self.bot.settings.bot_color,
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ControlsCog(bot))
