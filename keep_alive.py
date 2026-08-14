"""keep_alive.py — Server aiohttp para healthchecks de Render + diagnostico.

Endpoints:
    GET /            — Home (JSON)
    GET /health      — Healthcheck (devuelve 200 si el bot vive, 503 si Lavalink cae)
    GET /status      — Estado del bot
    GET /diag        — Diagnostico completo (Lavalink + circuit breaker + players)
    GET /diag/ytdlp  — Test del fallback yt-dlp
    GET /lavalinks   — Log completo de Lavalink
    GET /botlogs     — Log completo del bot
"""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

LOGGER = logging.getLogger("aisak.keep_alive")
_APP: web.Application | None = None
_RUNNER: web.AppRunner | None = None
_RUNNER_PORT: int | None = None
_BOT_REF: Any = None


def set_bot_ref(bot: Any) -> None:
    global _BOT_REF
    _BOT_REF = bot


def _runtime() -> dict[str, Any]:
    if _BOT_REF is None:
        return {}
    return {
        "bot_status": _BOT_REF.runtime.bot_status,
        "discord_gateway": _BOT_REF.runtime.discord_gateway,
        "lavalink_connected": _BOT_REF.runtime.lavalink_connected,
        "detail": _BOT_REF.runtime.detail,
    }


# ---------------------------------------------------------------------- #
# Handlers
# ---------------------------------------------------------------------- #
async def _home(request: web.Request) -> web.Response:
    return web.json_response({"service": "AISAK", "status": _runtime().get("bot_status", "starting")})


async def _health(request: web.Request) -> web.Response:
    rt = _runtime()
    bot_alive = rt.get("bot_status") not in {None, "", "error"}
    lavalink_ok = bool(rt.get("lavalink_connected"))
    # Render requiere 200 durante el healthcheck del deploy. Mientras el
    # proceso este vivo y arrancando, devolvemos 200 con detail explicando
    # el estado. Solo devolvemos 503 si el bot murio (bot_status=error) o
    # si Lavalink esta caido despues del warmup period (handled por monitor).
    if not bot_alive:
        return web.json_response(
            {
                "health": "error",
                "bot_status": rt.get("bot_status"),
                "lavalink": "connected" if lavalink_ok else "disconnected",
                "detail": rt.get("detail"),
            },
            status=503,
        )
    return web.json_response(
        {
            "health": "ok" if lavalink_ok else "starting",
            "bot_status": rt.get("bot_status"),
            "discord_gateway": rt.get("discord_gateway"),
            "lavalink": "connected" if lavalink_ok else "disconnected",
            "detail": rt.get("detail"),
        },
        status=200,
    )


async def _status(request: web.Request) -> web.Response:
    return web.json_response({"service": "AISAK", **_runtime()})


async def _diag(request: web.Request) -> web.Response:
    result: dict[str, Any] = {}

    # 1) Lavalink /v4/info
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
            async with s.get(
                "http://127.0.0.1:2333/v4/info",
                headers={"Authorization": "youshallnotpass"},
            ) as resp:
                info = await resp.json()
                result["lavalink_info"] = {
                    "version": info.get("version", {}).get("semver"),
                    "plugins": [
                        {"name": p.get("name"), "version": p.get("version")}
                        for p in info.get("plugins", [])
                    ],
                }
    except Exception as exc:
        result["lavalink_info_error"] = str(exc)

    # 2) Lavalink search test (youtube-plugin)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            identifier = urllib.parse.quote("ytsearch:never gonna give you up", safe="")
            async with s.get(
                f"http://127.0.0.1:2333/v4/loadtracks?identifier={identifier}",
                headers={"Authorization": "youshallnotpass"},
            ) as resp:
                data = await resp.json()
                lt = data.get("loadType", "unknown")
                result["lavalink_search"] = {"loadType": lt, "http_status": resp.status}
                if lt == "search":
                    tracks = data.get("data", [])
                    if isinstance(tracks, dict):
                        tracks = tracks.get("tracks", [])
                    result["lavalink_search"]["count"] = len(tracks)
                elif lt == "error":
                    result["lavalink_search"]["error"] = str(data.get("data", {}))[:300]
    except Exception as exc:
        result["lavalink_search_error"] = str(exc)

    # 3) Circuit breaker + OAuth config
    if _BOT_REF is not None:
        lavalink = getattr(_BOT_REF, "lavalink", None)
        if lavalink is not None:
            try:
                result["circuit_breaker"] = {
                    "tripped": lavalink.breaker_is_tripped(),
                    "consecutive_failures": lavalink._consecutive_failures,
                }
            except Exception as exc:
                result["circuit_breaker_error"] = str(exc)
            try:
                lv_settings = getattr(lavalink, "settings", None)
                result["oauth_configured"] = bool(
                    getattr(lv_settings, "oauth_configured", False)
                ) if lv_settings else False
            except Exception:
                result["oauth_configured"] = False

    # 4) Players activos
    if _BOT_REF is not None:
        music = getattr(_BOT_REF, "music", None)
        if music:
            players: dict[str, Any] = {}
            for gid, state in music.states.items():
                p = state.player
                if p:
                    vc = getattr(p, "channel", None)
                    players[str(gid)] = {
                        "playing": getattr(p, "playing", False),
                        "paused": getattr(p, "paused", False),
                        "position": getattr(p, "position", 0),
                        "connected": getattr(p, "connected", False),
                        "channel": getattr(vc, "name", None) if vc else None,
                        "current_title": getattr(state.current, "title", None),
                        "current_uri": getattr(state.current, "webpage_url", None),
                        "queue_size": len(state.queue),
                    }
            result["players"] = players

    return web.json_response(result)


async def _diag_ytdlp(request: web.Request) -> web.Response:
    if _BOT_REF is None:
        return web.json_response({"error": "bot not ready"}, status=503)
    lavalink = getattr(_BOT_REF, "lavalink", None)
    if lavalink is None:
        return web.json_response({"error": "lavalink manager not ready"}, status=503)
    query = request.query.get("q", "never gonna give you up")
    try:
        tracks = await asyncio.wait_for(
            lavalink._ytdlp_search(query, "diag", 0, 3),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "yt-dlp timeout", "query": query}, status=504)
    except Exception as exc:
        return web.json_response({"error": str(exc), "query": query}, status=500)
    return web.json_response({
        "query": query,
        "count": len(tracks),
        "tracks": [
            {
                "title": t.title,
                "webpage_url": t.webpage_url,
                "has_stream_url": bool(t.stream_url),
                "duration": t.duration,
                "uploader": t.uploader,
            }
            for t in tracks
        ],
    })


async def _logs_lavalink(request: web.Request) -> web.Response:
    log_path = "/tmp/lavalink.log"
    if not os.path.exists(log_path):
        return web.json_response({"error": "lavalink.log not found"}, status=404)
    try:
        content = Path(log_path).read_text(errors="replace")
        return web.Response(text=content, content_type="text/plain", charset="utf-8")
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def _logs_bot(request: web.Request) -> web.Response:
    # No tenemos archivo de log del bot, devolvemos ultima linea conocida.
    return web.json_response({"message": "Bot logs are in stdout (Render dashboard)."})


# ---------------------------------------------------------------------- #
# App setup
# ---------------------------------------------------------------------- #

async def _memory_stats(request: web.Request) -> web.Response:
    """Endpoint de diagnóstico de memoria (MEJORA AGREGADA)."""
    import gc
    import sys
    
    stats = {
        "gc_objects": len(gc.get_objects()),
        "gc_garbage": len(gc.garbage),
        "python_version": sys.version.split()[0],
    }
    
    if _BOT_REF is not None:
        music = getattr(_BOT_REF, "music", None)
        if music:
            stats["active_guilds"] = len(music.states)
            stats["total_queue_items"] = sum(len(s.queue) for s in music.states.values())
        
        lavalink = getattr(_BOT_REF, "lavalink", None)
        if lavalink and hasattr(lavalink, "get_memory_usage"):
            try:
                stats["lavalink_memory"] = await lavalink.get_memory_usage()
            except Exception:
                pass
    
    return web.json_response(stats)

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", _home)
    app.router.add_get("/health", _health)
    app.router.add_get("/status", _status)
    app.router.add_get("/diag", _diag)
    app.router.add_get("/diag/ytdlp", _diag_ytdlp)
    app.router.add_get("/lavalinks", _logs_lavalink)
    app.router.add_get("/botlogs", _logs_bot)
    app.router.add_get("/memory", _memory_stats)
    return app


def init_app() -> None:
    global _APP
    _APP = create_app()
    LOGGER.info("App aiohttp creada (pendiente de iniciar en el event loop)")


async def start_server(port: int) -> None:
    global _RUNNER, _RUNNER_PORT
    if _APP is None:
        LOGGER.warning("keep_alive no inicializado — saltando server web")
        return
    if _RUNNER is not None and _RUNNER_PORT == port:
        LOGGER.info("Servidor web aiohttp ya estaba corriendo en puerto %s", port)
        return
    # Si hay un runner previo en otro puerto, cerrarlo limpiamente.
    if _RUNNER is not None:
        try:
            await _RUNNER.cleanup()
        except Exception:
            pass
        _RUNNER = None
    _RUNNER = web.AppRunner(_APP)
    _RUNNER_PORT = port
    await _RUNNER.setup()
    site = web.TCPSite(_RUNNER, "0.0.0.0", port)
    await site.start()
    LOGGER.info("Servidor web aiohttp iniciado en 0.0.0.0:%s", port)
