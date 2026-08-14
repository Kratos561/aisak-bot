"""utils/errors.py — Excepciones custom para AISAK.

Estas excepciones son atrapadas por el error handler global de Discord
(main.py) y se traducen a mensajes user-friendly.
"""
from __future__ import annotations


class AISAKError(Exception):
    """Base de todas las excepciones del bot."""


class ConfigurationError(AISAKError):
    """Falta una config obligatoria."""


class UserInputError(AISAKError):
    """El usuario metio un input invalido."""


class PermissionError(AISAKError):
    """El usuario no tiene permisos para hacer esto."""


class PlaybackError(AISAKError):
    """Algo fallo durante la reproduccion."""
