from __future__ import annotations

from urllib.parse import urlparse

from utils.errors import UserInputError


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
