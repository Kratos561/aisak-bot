"""utils/validators.py — Validacion de inputs del usuario."""
from __future__ import annotations

from urllib.parse import urlparse

from utils.errors import UserInputError
from utils.models import FilterPreset


MAX_QUERY_LENGTH = 300


def sanitize_query(query: str) -> str:
    s = " ".join(query.split()).strip()
    if not s:
        raise UserInputError("Debes indicar una cancion, artista o enlace valido.")
    if len(s) > MAX_QUERY_LENGTH:
        raise UserInputError(f"La busqueda no puede superar {MAX_QUERY_LENGTH} caracteres.")
    return s


def validate_volume(volume: int) -> int:
    if not 0 <= volume <= 100:
        raise UserInputError("El volumen debe estar entre 0 y 100.")
    return volume


def validate_speed(speed: float) -> float:
    if not 0.5 <= speed <= 2.0:
        raise UserInputError("La velocidad debe estar entre 0.5x y 2.0x.")
    return round(speed, 2)


def validate_pitch(semitones: int) -> int:
    if not -12 <= semitones <= 12:
        raise UserInputError("El pitch debe estar entre -12 y 12 semitonos.")
    return semitones


def validate_filter_preset(value: str) -> FilterPreset:
    try:
        return FilterPreset(value)
    except ValueError as exc:
        raise UserInputError("Ese preset de audio no esta soportado.") from exc


def validate_position(position: int) -> int:
    if position < 1:
        raise UserInputError("La posicion debe ser >= 1.")
    return position


def validate_skip_count(count: int) -> int:
    if count < 1:
        raise UserInputError("Debes saltar al menos 1 cancion.")
    return count


def is_url(value: str) -> bool:
    try:
        p = urlparse(value)
    except ValueError:
        return False
    return bool(p.scheme and p.netloc)


def is_connected(vc) -> bool:
    if vc is None:
        return False
    if hasattr(vc, "connected"):
        return bool(vc.connected)
    if hasattr(vc, "is_connected"):
        try:
            f = vc.is_connected
            return bool(f() if callable(f) else f)
        except Exception:
            return False
    return False
