from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.formatters import build_queue_embed, build_success_embed
from utils.models import RepeatMode
from utils.validators import validate_position


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="queue", description="Muestra la cola de reproduccion.")
    @app_commands.describe(page="Pagina de la cola")
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 50] = 1) -> None:
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_queue_embed(state, page, self.bot.settings.bot_color)
        )

    @app_commands.command(name="remove", description="Elimina una cancion de la cola por posicion.")
    @app_commands.describe(position="Numero de la cancion en la cola")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 100] = 1) -> None:
        removed = self.music.remove_from_queue(interaction.guild_id, validate_position(position))
        await interaction.response.send_message(
            embed=build_success_embed(
                "Cancion eliminada",
                f"Elimine **{removed.title}** de la cola.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="clear", description="Limpia toda la cola pendiente.")
    async def clear(self, interaction: discord.Interaction) -> None:
        cleared = self.music.clear_queue(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_success_embed(
                "Cola vaciada",
                f"Se eliminaron **{cleared}** canciones de la cola.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="shuffle", description="Mezcla aleatoriamente la cola.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        total = self.music.shuffle_queue(interaction.guild_id)
        await interaction.response.send_message(
            embed=build_success_embed(
                "Cola mezclada",
                f"Reordene **{total}** canciones de forma aleatoria.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="repeat", description="Cambia el modo de repeticion.")
    @app_commands.describe(mode="off, one o all")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="one", value="one"),
            app_commands.Choice(name="all", value="all"),
        ]
    )
    async def repeat(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        selected = self.music.set_repeat_mode(interaction.guild_id, RepeatMode(mode.value))
        await interaction.response.send_message(
            embed=build_success_embed(
                "Modo repeat actualizado",
                f"El loop quedo en **{selected.value}**.",
                self.bot.settings.bot_color,
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QueueCog(bot))
