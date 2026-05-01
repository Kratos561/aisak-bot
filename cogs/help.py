from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


COMMAND_DETAILS = {
    "play": "Busca por texto, URL de YouTube o Spotify y reproduce usando YouTube como unica fuente.",
    "playlist": "Agrega una playlist a la cola y respeta el orden (ideal para YouTube).",
    "soundcloud": "Alias de compatibilidad: ahora busca en YouTube.",
    "mixcloud": "Alias de compatibilidad: ahora solo acepta busquedas o enlaces de YouTube.",
    "youtube": "Fuerza una busqueda o reproduccion usando YouTube con ruta reforzada para 2026.",
    "pause": "Pausa la reproduccion actual.",
    "resume": "Reanuda la reproduccion pausada.",
    "skip": "Salta una o varias canciones.",
    "queue": "Muestra la cola actual paginada.",
    "nowplaying": "Muestra la cancion actual, su progreso y el panel de controles.",
    "remove": "Elimina una cancion de la cola por posicion.",
    "clear": "Vacia toda la cola pendiente.",
    "shuffle": "Mezcla el orden de las canciones en cola.",
    "repeat": "Cambia el modo de repeticion entre off, one y all.",
    "volume": "Ajusta el volumen de salida del bot.",
    "speed": "Cambia la velocidad base entre 0.5x y 2.0x y refresca la pista actual si hace falta.",
    "pitch": "Sube o baja el pitch entre -12 y 12 semitonos.",
    "filter": "Aplica presets como bassboost, clear, radio, nightcore o vaporwave.",
    "effectsreset": "Restaura velocidad, pitch y preset al perfil normal.",
    "stop": "Detiene todo y desconecta el bot.",
    "search": "Busca resultados en YouTube y te deja elegir uno.",
    "lyrics": "Intenta obtener la letra de la cancion actual.",
    "help": "Muestra este resumen o el detalle de un comando.",
}


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="Muestra la lista de comandos o ayuda detallada.")
    @app_commands.describe(command="Nombre exacto del comando")
    async def help(self, interaction: discord.Interaction, command: str | None = None) -> None:
        if command:
            key = command.lower().strip().lstrip("/")
            detail = COMMAND_DETAILS.get(key)
            if detail is None:
                await interaction.response.send_message(
                    f"No conozco el comando `{command}`.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"/{key}",
                description=detail,
                color=self.bot.settings.bot_color,
            )
            embed.set_footer(text="AISAK | Slash commands sincronizados con Discord")
            await interaction.response.send_message(embed=embed)
            return

        lines = [f"`/{name}` - {description}" for name, description in COMMAND_DETAILS.items()]
        embed = discord.Embed(
            title="Comandos de AISAK",
            description="\n".join(lines),
            color=self.bot.settings.bot_color,
        )
        embed.add_field(
            name="Consejos",
            value=(
                "- Usa `/play` con texto o enlaces.\n"
                "- Mientras escribes `/play`, AISAK te sugerira coincidencias.\n"
                "- AISAK reproduce desde YouTube como fuente unica.\n"
                "- Usa `/playlist` con enlaces de playlist de YouTube.\n"
                "- Usa `/speed`, `/pitch`, `/filter` y `/effectsreset` para moldear el audio.\n"
                "- Usa los botones de Pause, Skip, Stop, AutoPlay, Dashboard y Like debajo del panel."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
