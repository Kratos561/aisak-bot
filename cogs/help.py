"""cogs/help.py — /help."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.ui import success_embed


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.music = bot.music

    @app_commands.command(name="help", description="Lista de comandos de AISAK.")
    async def help(self, interaction: discord.Interaction) -> None:
        desc = """
**Reproduccion**
`/play <query>` — Reproduce una cancion o URL de YouTube
`/pause` — Pausa la cancion actual
`/resume` — Reanuda la reproduccion
`/skip [count]` — Salta 1 o varias canciones
`/stop` — Detiene y desconecta el bot

**Cola**
`/queue [page]` — Muestra la cola
`/remove <pos>` — Quita una cancion por posicion
`/clear` — Vacia la cola
`/shuffle` — Mezcla la cola
`/repeat <mode>` — Activa repeticion (off/one/all)
`/nowplaying` — Muestra la cancion actual con progreso

**Audio**
`/volume <0-100>` — Cambia el volumen
`/speed <0.5-2.0>` — Cambia la velocidad
`/pitch <-12 a 12>` — Cambia el pitch en semitonos
`/filter <preset>` — Aplica bassboost/clear/radio/nightcore/vaporwave
`/effectsreset` — Restablece todo a normal

**Busqueda**
`/search <query>` — Busca y elige entre 5 resultados

Tambien puedes usar los botones del panel ahora sonando para controlar todo.
        """.strip()
        await interaction.response.send_message(
            embed=success_embed("Comandos de AISAK", desc, self.bot.settings.bot_color)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
