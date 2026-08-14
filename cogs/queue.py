"""cogs/queue.py — Comandos de cola: queue, remove, clear, shuffle, repeat, nowplaying."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.models import MessageRef, RepeatMode
from utils.ui import PlayerControlsView, now_playing_embed, queue_embed, success_embed
from utils.validators import validate_position


REPEAT_CHOICES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="one", value="one"),
    app_commands.Choice(name="all", value="all"),
]


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="queue", description="Muestra la cola de reproduccion.")
    @app_commands.describe(page="Pagina de la cola (default 1)")
    async def queue(
        self,
        interaction: discord.Interaction,
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        state = self.music.get_state(interaction.guild_id)
        await interaction.response.send_message(
            embed=queue_embed(state, page, self.bot.settings.bot_color)
        )

    @app_commands.command(name="remove", description="Quita una cancion de la cola por posicion.")
    @app_commands.describe(position="Posicion en la cola (1 = primera)")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]) -> None:
        self.music.assert_control_access(interaction)
        removed = self.music.remove_from_queue(interaction.guild_id, validate_position(position))
        await interaction.response.send_message(
            embed=success_embed(
                "Cancion quitada",
                f"Saqué **{removed.title}** de la cola.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="clear", description="Vacia la cola de reproduccion.")
    async def clear(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        n = self.music.clear_queue(interaction.guild_id)
        await interaction.response.send_message(
            embed=success_embed("Cola vacia", f"Saqué **{n}** canciones.", self.bot.settings.bot_color)
        )

    @app_commands.command(name="shuffle", description="Mezcla la cola actual.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        n = self.music.shuffle_queue(interaction.guild_id)
        await interaction.response.send_message(
            embed=success_embed("Cola mezclada", f"Mezcle **{n}** canciones.", self.bot.settings.bot_color)
        )

    @app_commands.command(name="repeat", description="Activa repeticion de cola o cancion.")
    @app_commands.choices(mode=REPEAT_CHOICES)
    async def repeat(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ) -> None:
        self.music.assert_control_access(interaction)
        rm = self.music.set_repeat(interaction.guild_id, RepeatMode(mode.value))
        labels = {"off": "Desactivada", "one": "Una cancion", "all": "Toda la cola"}
        await interaction.response.send_message(
            embed=success_embed(
                "Repeticion actualizada",
                f"Modo: **{labels.get(rm.value, rm.value)}**.",
                self.bot.settings.bot_color,
            )
        )

    @app_commands.command(name="nowplaying", description="Muestra la cancion actual con progreso.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        self.music.assert_control_access(interaction)
        state = self.music.get_state(interaction.guild_id)
        view = PlayerControlsView(self.bot, interaction.guild_id) if state.current else None
        await interaction.response.send_message(
            embed=now_playing_embed(state, self.bot.settings.bot_color),
            view=view,
        )
        if state.current:
            message = await interaction.original_response()
            state.active_panel = MessageRef(channel_id=message.channel.id, message_id=message.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QueueCog(bot))
