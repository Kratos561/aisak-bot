from __future__ import annotations

import os
import sys
from typing import Any

import requests


API_BASE = "https://api.uptimerobot.com/v2"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"Falta la variable de entorno {name}.", file=sys.stderr)
        raise SystemExit(1)
    return value


def post(endpoint: str, api_key: str, **payload: Any) -> dict[str, Any]:
    response = requests.post(
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
    api_key = require_env("UPTIMEROBOT_API_KEY")
    target_url = os.getenv("TARGET_URL", "").strip() or require_env("HF_SPACE_URL")
    monitor_name = os.getenv("UPTIMEROBOT_MONITOR_NAME", "AISAK Bot").strip() or "AISAK Bot"

    existing = post("getMonitors", api_key, logs=0)
    monitors = existing.get("monitors", [])

    for monitor in monitors:
        if monitor.get("friendly_name") == monitor_name:
            post(
                "editMonitor",
                api_key,
                id=monitor["id"],
                friendly_name=monitor_name,
                url=target_url,
                type=1,
                interval=300,
            )
            print(f"Monitor actualizado: {monitor_name}")
            return

    post(
        "newMonitor",
        api_key,
        friendly_name=monitor_name,
        url=target_url,
        type=1,
        interval=300,
    )
    print(f"Monitor creado: {monitor_name}")


if __name__ == "__main__":
    main()
