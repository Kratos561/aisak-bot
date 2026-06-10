from __future__ import annotations

import os
import sys
from typing import Any

import requests


API_BASE = "https://api.uptimerobot.com/v2"
INVALID_LOCAL_PROXIES = {
    "http://127.0.0.1:9",
    "https://127.0.0.1:9",
    "http://localhost:9",
    "https://localhost:9",
}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"Falta la variable de entorno {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


def create_session() -> requests.Session:
    session = requests.Session()
    for proxy_key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        proxy_value = os.getenv(proxy_key, "").strip()
        if proxy_value in INVALID_LOCAL_PROXIES:
            session.trust_env = False
            break
    return session


def post(session: requests.Session, endpoint: str, api_key: str, **payload: Any) -> dict[str, Any]:
    response = session.post(
        f"{API_BASE}/{endpoint}",
        data={"api_key": api_key, "format": "json", **payload},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    stat = data.get("stat")
    if stat != "ok":
        raise RuntimeError(f"UptimeRobot devolvio un error: {data}")
    return data


def main() -> None:
    session = create_session()
    api_key = require_env("UPTIMEROBOT_API_KEY")
    target_url = os.getenv("TARGET_URL", "").strip() or require_env("HF_SPACE_URL")
    monitor_name = os.getenv("UPTIMEROBOT_MONITOR_NAME", "AISAK Bot").strip() or "AISAK Bot"
    interval = os.getenv("UPTIMEROBOT_INTERVAL", "300").strip() or "300"

    existing = post(session, "getMonitors", api_key, logs=0)
    monitors = existing.get("monitors", [])

    for monitor in monitors:
        if monitor.get("friendly_name") == monitor_name:
            post(
                session,
                "editMonitor",
                api_key,
                id=monitor["id"],
                friendly_name=monitor_name,
                url=target_url,
                type=1,
                interval=interval,
            )
            print(f"Monitor actualizado: {monitor_name}")
            return

    post(
        session,
        "newMonitor",
        api_key,
        friendly_name=monitor_name,
        url=target_url,
        type=1,
        interval=interval,
    )
    print(f"Monitor creado: {monitor_name}")


if __name__ == "__main__":
    main()
