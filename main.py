"""main.py — Entry point de AISAK v2.

Crea el bot, registra cogs, conecta Lavalink, arranca el server web de
healthcheck. Si el bot muere, retry con backoff.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from config import get_settings
from keep_alive import init_app, set_bot_ref, start_server
from utils.errors import (
    AISAKError,
    ConfigurationError,
    PermissionError as AISAKPermissionError,
    PlaybackError,
    UserInputError,
)
from utils.logger import configure_logging
from utils.models import RuntimeStatus
from utils.ui import PlayerControlsView, error_embed

EXTENSIONS = [
    "cogs.music",
    "cogs.queue",
    "cogs.controls",
    "cogs.search",
    "cogs.help",
]


class AISAKBot(commands.Bot):
    def __init__(self) -> None:
        load_dotenv()
        self.settings = get_settings()
        self.logger = configure_logging(self.settings)
        self.runtime = RuntimeStatus()
        self._guild_sync_lock = asyncio.Lock()
        self._runtime_guild_sync_done = False

        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(command_prefix="!", intents=intents)

        # Managers (se inicializan despues de super().__init__ porque
        # registran listeners en self).
        from utils.lavalink import LavalinkManager
        from utils.music import MusicManager
        self.lavalink = LavalinkManager(self, self.settings, self.logger)
        self.music = MusicManager(self, self.settings, self.logger)

    async def setup_hook(self) -> None:
        # Server web primero (Render healthcheck debe responder rapido).
        await start_server(self.settings.web_port)
        set_bot_ref(self)

        # Panel interactivo persistente (sobrevive a reinicios del bot).
        # guild_id=0 es un SENTINEL: esta vista solo existe para que discord.py
        # reconozca los custom_id "aisak:*" de paneles creados antes de un
        # reinicio. El guild_id real de cada interaccion se toma de
        # interaction.guild_id dentro de _delegate(), no de este atributo.
        self.add_view(PlayerControlsView(self, 0))

        # Cargar cogs.
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            self.logger.info("Cog cargado: %s", ext)

        # Sincronizar slash commands.
        await self.tree.sync()
        self.logger.info("Slash commands globales sincronizados.")
        for gid in self.settings.test_guild_ids:
            try:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                self.logger.info("Slash commands en guild %s: %d", gid, len(synced))
            except Exception as exc:
                self.logger.warning("Sync guild %s fallo: %s", gid, exc)

        # Background tasks.
        self.loop.create_task(self._connect_lavalink_task())
        self.loop.create_task(self._warmup_task())
        self.loop.create_task(self._monitor_task())
        self.loop.create_task(self._memory_cleanup_task())

    async def _connect_lavalink_task(self) -> None:
        await self.wait_until_ready()
        last_error = "desconocido"
        for attempt in range(60):
            try:
                # Limpiar nodos zombi antes de reintentar: Pool.connect()
                # agrega el nodo aunque falle, asi que sin limpieza acumulamos
                # nodos rotos y el siguiente intento falla por InvalidNode.
                try:
                    from wavelink import Pool
                    for existing in list(Pool.nodes.values()):
                        try:
                            await existing.close(eject=True)
                        except Exception:
                            pass
                except Exception:
                    pass
                await self.lavalink.connect()
                self.logger.info("Lavalink conectado.")
                self.runtime.bot_status = "online"
                self.runtime.discord_gateway = "connected"
                self.runtime.lavalink_connected = True
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.logger.warning("Lavalink intento %d/60: %s", attempt + 1, last_error)
                await asyncio.sleep(3)
        self.logger.error("No pude conectar a Lavalink tras 60 intentos: %s", last_error)
        self.runtime.lavalink_connected = False
        self.runtime.detail = f"Lavalink: {last_error}"

    async def _warmup_task(self) -> None:
        await asyncio.sleep(10)
        try:
            result = await self.lavalink.warmup()
            if result == "ok":
                self.logger.info("Warmup OK: youtube-plugin funcional.")
            elif result == "url_only":
                self.logger.warning("Warmup parcial: search falla, URLs funcionan.")
            elif result == "ytdlp":
                self.logger.warning("Warmup: plugin caido, fallback yt-dlp OK.")
            else:
                self.logger.error("Warmup FALLO: ni plugin ni yt-dlp funcionan.")
        except Exception as exc:
            self.logger.warning("Warmup excepcion: %s", exc)

    async def _monitor_task(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.sleep(30)
                healthy = await self.lavalink.check_health()
                if not healthy:
                    self.logger.warning("Lavalink health check negativo.")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning("Monitor error: %s", exc)


    async def _memory_cleanup_task(self) -> None:
        """Task de fondo que limpia estados inactivos cada 30 minutos.
        
        Previene memory leaks en bots de larga duración limpiando
        GuildMusicState de servidores que no han tenido actividad
        en más de 1 hora.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.sleep(1800)  # 30 minutos
                if hasattr(self, 'music') and self.music:
                    cleaned = await self.music.cleanup_inactive_states()
                    if cleaned > 0:
                        self.logger.info(f"Memory cleanup: {cleaned} estados limpiados")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning(f"Memory cleanup task error: {exc}")

    async def on_ready(self) -> None:
        if self.user is None:
            return
        self.runtime.bot_status = "online"
        self.runtime.discord_gateway = "connected"
        self.runtime.detail = f"Conectado como {self.user} ({self.user.id})"
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening,
                    name="/help para ver comandos",
                )
            )
        except (discord.HTTPException, discord.ConnectionError) as exc:
            # change_presence puede fallar durante un resume transitorio.
            self.logger.warning("change_presence fallo (no critico): %s", exc)
        self.logger.info("AISAK online como %s (%s)", self.user, self.user.id)

    async def on_connect(self) -> None:
        self.runtime.bot_status = "connecting"
        self.runtime.discord_gateway = "connecting"

    async def on_disconnect(self) -> None:
        self.runtime.bot_status = "reconnecting"
        self.runtime.discord_gateway = "disconnected"
        self.logger.warning("Discord gateway desconectado. discord.py intentara reconectar.")

    async def on_resumed(self) -> None:
        if self.user:
            self.runtime.bot_status = "online"
            self.runtime.discord_gateway = "connected"
            self.logger.info("Sesion Discord resumida OK.")


def install_error_handler(bot: AISAKBot) -> None:
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        actual = getattr(error, "original", error)
        if isinstance(actual, (AISAKError, UserInputError, PlaybackError, ConfigurationError, AISAKPermissionError)):
            message = str(actual)
        elif isinstance(actual, app_commands.CommandNotFound):
            return  # slash command desconocido: ignorar silenciosamente
        elif isinstance(actual, app_commands.CheckFailure):
            message = "No tienes permisos para usar este comando."
        elif isinstance(actual, app_commands.CommandOnCooldown):
            message = f"Espera {actual.retry_after:.1f}s antes de reintentar."
        elif isinstance(actual, discord.Forbidden):
            message = "Discord rechazo la operacion. Revisa permisos del bot."
        elif isinstance(actual, discord.NotFound):
            message = "El recurso ya no existe (puede que haya sido borrado)."
        elif isinstance(actual, (aiohttp.ClientError, OSError)):
            message = "Error de red. Reintentando automaticamente."
            bot.logger.warning("Error de red en comando: %s", actual)
        else:
            message = f"Error interno: {type(actual).__name__}: {actual}"
            bot.logger.exception("Error en comando: %s", message)
        embed = error_embed(message, bot.settings.bot_color)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass


def log_versions(bot: AISAKBot) -> None:
    for label, name in [("discord.py", "discord.py"), ("wavelink", "wavelink")]:
        try:
            v = importlib.metadata.version(name)
            bot.logger.info("%s version: %s", label, v)
        except importlib.metadata.PackageNotFoundError:
            bot.logger.warning("%s no instalado.", label)


def ensure_dirs_and_env(bot: AISAKBot) -> None:
    if not bot.settings.discord_token:
        raise ConfigurationError("Falta DISCORD_TOKEN.")
    Path("./data").mkdir(parents=True, exist_ok=True)
    log_versions(bot)
    oauth = bot.settings.oauth_configured
    if not oauth:
        missing = []
        if not bot.settings.youtube_oauth_refresh_token:
            missing.append("YOUTUBE_OAUTH_REFRESH_TOKEN")
        if not bot.settings.youtube_oauth_client_id:
            missing.append("YOUTUBE_OAUTH_CLIENT_ID")
        if not bot.settings.youtube_oauth_client_secret:
            missing.append("YOUTUBE_OAUTH_CLIENT_SECRET")
        bot.logger.warning(
            "OAuth de YouTube INCOMPLETO. Faltan: %s. "
            "El bot funcionara en modo anonimo y morira tras ~30 reproducciones/hora por rate-limit. "
            "URLs directas seguiran funcionando, pero busquedas por texto devolveran vacio.",
            ", ".join(missing),
        )


def main() -> None:
    settings = get_settings()
    if not settings.discord_token:
        raise ConfigurationError("Falta DISCORD_TOKEN. Copia .env.example a .env y configuralo.")

    init_app()

    while True:
        bot = AISAKBot()
        install_error_handler(bot)
        ensure_dirs_and_env(bot)
        bot.runtime.bot_status = "connecting"
        bot.runtime.detail = "Conectando con Discord..."
        try:
            bot.run(settings.discord_token, log_handler=None)
            break
        except discord.LoginFailure:
            bot.runtime.bot_status = "error"
            bot.runtime.detail = "Token de Discord invalido."
            raise
        except (aiohttp.ClientError, OSError, discord.HTTPException) as exc:
            delay = 20
            bot.logger.exception("Discord fallo. Reintentando en %ss.", delay)
            bot.runtime.bot_status = "retrying"
            bot.runtime.detail = f"{type(exc).__name__}: {exc}"
            time.sleep(delay)


if __name__ == "__main__":
    main()
