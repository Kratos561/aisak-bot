from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import subprocess
import time
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from config import get_settings
from keep_alive import keep_alive, set_runtime_status
from utils.errors import AISAKError, ConfigurationError, PermissionError as AISAKPermissionError, PlaybackError, UserInputError
from utils.formatters import build_error_embed
from utils.logger import configure_logging
from utils.music_manager import MusicManager
from utils.player_controls import PlayerControlsView

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
        self._guild_sync_lock = asyncio.Lock()
        self._runtime_guild_sync_completed = False

        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(command_prefix=self.settings.bot_prefix, intents=intents)
        self.music = MusicManager(self, self.settings, self.logger)

    async def setup_hook(self) -> None:
        self.add_view(PlayerControlsView(self))
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            self.logger.info("Cog cargado: %s", extension)

        await self.sync_global_commands()
        await self.sync_configured_guild_commands()

    async def on_ready(self) -> None:
        if self.user is None:
            return
        set_runtime_status(
            bot_status="online",
            connected=True,
            detail=f"Conectado como {self.user} ({self.user.id})",
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/help para ver comandos",
            )
        )
        self.logger.info("AISAK conectado como %s (%s)", self.user, self.user.id)
        await self.sync_connected_guild_commands()

    async def on_connect(self) -> None:
        set_runtime_status(
            bot_status="connecting",
            connected=False,
            detail="Conexion inicial con Discord establecida; esperando ready.",
        )

    async def on_disconnect(self) -> None:
        set_runtime_status(
            bot_status="reconnecting",
            connected=False,
            detail="Gateway de Discord desconectado; esperando reconexion.",
        )

    async def on_resumed(self) -> None:
        if self.user is None:
            return
        set_runtime_status(
            bot_status="online",
            connected=True,
            detail=f"Sesion reanudada para {self.user} ({self.user.id})",
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user and member.id == self.user.id:
            self.logger.info(
                "Cambio de voz del bot: guild=%s before=%s after=%s",
                getattr(member.guild, "id", "unknown"),
                getattr(getattr(before, "channel", None), "id", None),
                getattr(getattr(after, "channel", None), "id", None),
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_guild_commands(guild.id, reason="nuevo guild")

    async def sync_global_commands(self) -> None:
        synced = await self.tree.sync()
        self.logger.info("Slash commands globales sincronizados: %s", len(synced))

    async def sync_configured_guild_commands(self) -> None:
        for guild_id in self.settings.test_guild_ids:
            await self.sync_guild_commands(guild_id, reason="guild configurado")

    async def sync_connected_guild_commands(self) -> None:
        if self._runtime_guild_sync_completed:
            return

        guild_ids = {guild.id for guild in self.guilds}
        guild_ids.update(self.settings.test_guild_ids)

        if not guild_ids:
            self._runtime_guild_sync_completed = True
            self.logger.info("No hay guilds conectados para sincronizar comandos de forma inmediata.")
            return

        for guild_id in sorted(guild_ids):
            await self.sync_guild_commands(guild_id, reason="guild conectado")

        self._runtime_guild_sync_completed = True

    async def sync_guild_commands(self, guild_id: int, *, reason: str) -> None:
        async with self._guild_sync_lock:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.logger.info(
                "Slash commands sincronizados para %s %s: %s",
                reason,
                guild_id,
                len(synced),
            )


def install_error_handler(bot: AISAKBot) -> None:
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)

        if isinstance(original, (AISAKError, UserInputError, PlaybackError, ConfigurationError, AISAKPermissionError)):
            message = str(original)
        elif isinstance(original, discord.Forbidden):
            message = "Discord rechazo la operacion. Revisa permisos del bot."
        elif isinstance(original, app_commands.CommandInvokeError):
            message = "El comando fallo internamente. Revisa los logs para mas detalles."
            bot.logger.exception("Error inesperado invocando slash command", exc_info=original)
        else:
            message = "Ocurrio un error inesperado procesando ese comando."
            bot.logger.exception("Error de aplicacion no controlado", exc_info=original)

        embed = build_error_embed(message, bot.settings.bot_color)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


def ensure_environment(bot: AISAKBot) -> None:
    if not bot.settings.discord_token:
        raise ConfigurationError("Falta DISCORD_TOKEN en el entorno o en el archivo .env.")

    Path("./data/cache").mkdir(parents=True, exist_ok=True)
    Path("./data/logs").mkdir(parents=True, exist_ok=True)
    ensure_opus_loaded(bot)
    log_runtime_versions(bot)


def ensure_opus_loaded(bot: AISAKBot) -> None:
    if discord.opus.is_loaded():
        return

    for candidate in ("libopus.so.0", "libopus.so", "opus", "libopus-0.x64.dll", "libopus-0.dll"):
        try:
            discord.opus.load_opus(candidate)
            bot.logger.info("Opus cargado desde %s", candidate)
            return
        except OSError:
            continue

    bot.logger.warning("No pude cargar Opus explicitamente. Si la voz falla, revisa la libreria libopus del sistema.")


def log_runtime_versions(bot: AISAKBot) -> None:
    packages = {
        "discord.py": "discord.py",
        "davey": "davey",
        "yt-dlp": "yt-dlp",
    }
    for label, package_name in packages.items():
        try:
            version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            bot.logger.warning("%s no esta instalado en el entorno activo.", label)
        else:
            bot.logger.info("%s version detectada: %s", label, version)

    for runtime_name, command in (
        ("node", ["node", "--version"]),
        ("npm", ["npm", "--version"]),
    ):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)
        except (FileNotFoundError, subprocess.SubprocessError):
            bot.logger.warning("%s no esta disponible en el entorno activo.", runtime_name)
        else:
            bot.logger.info("%s version detectada: %s", runtime_name, completed.stdout.strip())


def main() -> None:
    base_settings = get_settings()
    if not base_settings.discord_token:
        raise ConfigurationError("Falta DISCORD_TOKEN en el entorno o en el archivo .env.")

    keep_alive(base_settings)

    while True:
        bot = AISAKBot()
        install_error_handler(bot)
        ensure_environment(bot)
        set_runtime_status(
            bot_status="connecting",
            connected=False,
            detail="Intentando conectar con Discord.",
        )
        try:
            bot.run(bot.settings.discord_token, log_handler=None)
            set_runtime_status(
                bot_status="stopped",
                connected=False,
                detail="El bot se detuvo.",
            )
            break
        except discord.LoginFailure:
            set_runtime_status(
                bot_status="error",
                connected=False,
                detail="El token de Discord es invalido o fue revocado.",
            )
            raise
        except (aiohttp.ClientError, OSError, discord.HTTPException) as exc:
            retry_delay = max(5, bot.settings.discord_retry_delay)
            bot.logger.exception(
                "No pude conectar con Discord. Reintentare en %s segundos.",
                retry_delay,
            )
            set_runtime_status(
                bot_status="retrying",
                connected=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
