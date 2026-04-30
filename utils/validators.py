from __future__ import annotations

from urllib.parse import urlparse

from utils.errors import UserInputError
from utils.models import AudioFilterPreset


MAX_QUERY_LENGTH = 300


def sanitize_query(query: str) -> str:
    normalized = " ".join(query.split()).strip()
    if not normalized:
        raise UserInputError("Debes indicar una cancion, artista o enlace valido.")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise UserInputError(f"La busqueda no puede superar {MAX_QUERY_LENGTH} caracteres.")
    return normalized


def validate_volume(volume: int) -> int:
    if volume < 0 or volume > 100:
        raise UserInputError("El volumen debe estar entre 0 y 100.")
    return volume


def validate_speed(speed: float) -> float:
    if speed < 0.5 or speed > 2.0:
        raise UserInputError("La velocidad debe estar entre 0.5x y 2.0x.")
    return round(speed, 2)


def validate_pitch(semitones: int) -> int:
    if semitones < -12 or semitones > 12:
        raise UserInputError("El pitch debe estar entre -12 y 12 semitonos.")
    return semitones


def validate_filter_preset(raw_value: str) -> AudioFilterPreset:
    try:
        return AudioFilterPreset(raw_value)
    except ValueError as exc:
        raise UserInputError("Ese preset de audio no esta soportado.") from exc


def validate_position(position: int) -> int:
    if position < 1:
        raise UserInputError("La posicion debe ser mayor o igual a 1.")
    return position


def validate_skip_count(count: int) -> int:
    if count < 1:
        raise UserInputError("Debes saltar al menos 1 cancion.")
    return count


def is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return bool(parsed.scheme and parsed.netloc)
