"""utils/logger.py — Logging estructurado para AISAK v2.

Forma de uso:
    from utils.logger import get_logger
    log = get_logger(__name__)
    log.info("Bot listo")
"""
from __future__ import annotations

import logging
import sys

from config import Settings

_LOGGER_CONFIGURED = False


def configure_logging(settings: Settings) -> logging.Logger:
    global _LOGGER_CONFIGURED
    if _LOGGER_CONFIGURED:
        return logging.getLogger("aisak")

    level = getattr(logging, settings.log_level, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Quitar handlers por defecto para no duplicar.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    # Formato simple y parseable. Render ya anade timestamp automaticamente.
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)

    # Silenciar librerias muy ruidosas.
    for noisy in ("discord", "discord.http", "discord.gateway", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGER_CONFIGURED = True
    return logging.getLogger("aisak")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
