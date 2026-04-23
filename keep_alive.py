from __future__ import annotations

import logging
from threading import Thread
from typing import Any

from flask import Flask, jsonify

from config import Settings

LOGGER = logging.getLogger("aisak.keep_alive")
APP = Flask(__name__)
_SERVER_THREAD: Thread | None = None
_RUNTIME_STATE: dict[str, Any] = {
    "service": "AISAK",
    "web_status": "online",
    "bot_status": "starting",
    "discord_gateway": "disconnected",
    "detail": "Inicializando",
}


def set_runtime_status(*, bot_status: str, connected: bool, detail: str | None = None) -> None:
    _RUNTIME_STATE["bot_status"] = bot_status
    _RUNTIME_STATE["discord_gateway"] = "connected" if connected else "disconnected"
    if detail is not None:
        _RUNTIME_STATE["detail"] = detail


@APP.get("/")
def home() -> tuple[dict[str, Any], int]:
    return (
        {
            "service": "AISAK",
            "status": _RUNTIME_STATE["bot_status"],
            "message": "AISAK Bot esta en linea",
        },
        200,
    )


@APP.get("/health")
def health() -> tuple[dict[str, Any], int]:
    healthy = _RUNTIME_STATE["discord_gateway"] == "connected"
    return (
        {
            "health": "ok" if healthy else "degraded",
            "bot_status": _RUNTIME_STATE["bot_status"],
            "discord_gateway": _RUNTIME_STATE["discord_gateway"],
            "detail": _RUNTIME_STATE["detail"],
        },
        200 if healthy else 503,
    )


@APP.get("/status")
def status() -> tuple[dict[str, Any], int]:
    return (
        {
            "bot": "AISAK",
            "status": _RUNTIME_STATE["bot_status"],
            "transport": "discord-gateway",
            "discord_gateway": _RUNTIME_STATE["discord_gateway"],
            "detail": _RUNTIME_STATE["detail"],
        },
        200,
    )


def keep_alive(settings: Settings) -> None:
    global _SERVER_THREAD

    if _SERVER_THREAD and _SERVER_THREAD.is_alive():
        return

    def run_flask() -> None:
        try:
            APP.run(host=settings.flask_host, port=settings.flask_port, debug=False, use_reloader=False)
        except Exception:  # pragma: no cover - logging path
            LOGGER.exception("No se pudo iniciar el servidor Flask.")

    _SERVER_THREAD = Thread(target=run_flask, daemon=True, name="aisak-flask")
    _SERVER_THREAD.start()
    LOGGER.info("Servidor Flask iniciado en %s:%s", settings.flask_host, settings.flask_port)
