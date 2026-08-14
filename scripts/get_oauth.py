#!/usr/bin/env python3
"""scripts/get_oauth.py — Obtiene un refresh token de YouTube para el
youtube-plugin de Lavalink.

Pasos:
  1. Crea una cuenta Google "burner" (NO uses tu cuenta personal).
     - Si pide verificar telefono, usa un numero temporal de sms-activate.org
       o 5sim.net ($0.05-$0.20). Nunca uses tu numero real.
  2. Inicia sesion con esa cuenta en un navegador.
  3. Ejecuta este script: `python scripts/get_oauth.py`
  4. Abre la URL que imprime el script en el navegador donde esta la burner.
  5. Autoriza el acceso. Google te redirige a http://localhost:PORT y este
     script captura el codigo automaticamente.
  6. El script imprime el refresh_token. Copialo y ponlo en Render:
       YOUTUBE_OAUTH_REFRESH_TOKEN=<token>
       YOUTUBE_OAUTH_CLIENT_ID=<CLIENT_ID abajo>
       YOUTUBE_OAUTH_CLIENT_SECRET=<CLIENT_SECRET abajo>

IMPORTANTE: Google deprecó el flujo OOB (urn:ietf:wg:oauth:2.0:oob) en 2022.
Ahora se requiere un redirect URI http://localhost. Este script levanta un
mini-servidor HTTP en el puerto 8080 para capturar el callback.

El client_id/secret usados son los del plugin youtube-source (extraidos del
JAR con `strings` — no son secretos, son el client de la app "TV" de YouTube
que el plugin usa internamente). Si cambias de version del plugin, debes
actualizar estos valores o el refresh_token no funcionara.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Client ID/secret del plugin youtube-source 1.13.0 (extraidos del JAR con
# `strings` — no son secretos, son el client de la app "TV" de YouTube que
# el plugin usa internamente). Si cambias de version del plugin, debes
# actualizar estos valores o el refresh_token no funcionara.
CLIENT_ID = "325763366291-j109oem3l0c0tknbiroap5u61275bgk6.apps.googleusercontent.com"
CLIENT_SECRET = "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"
SCOPE = "https://www.googleapis.com/auth/youtube"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"

# El codigo de autorizacion llega via el redirect del navegador. Lo captura
# el handler del mini-servidor y lo guarda aqui.
_auth_code: dict[str, str | None] = {"code": None}


class _OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self, request):  # noqa: N802 — stdlib API name
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        err = params.get("error", [None])[0]

        if code:
            _auth_code["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
                b"<h2>&#10004; Autorizacion recibida</h2>"
                b"<p>Puedes cerrar esta pestana y volver a la terminal.</p>"
                b"</body></html>"
            )
        else:
            msg = err or "Falto el parametro 'code'."
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body><h2>Error: {msg}</h2></body></html>".encode("utf-8"))

    def log_message(self, *args):  # silenciar logs del stdlib
        pass


def _wait_for_code(timeout: float = 300.0) -> str | None:
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _OAuthHandler)
    server.timeout = 1
    deadline = _now() + timeout
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while _now() < deadline and _auth_code["code"] is None:
            pass
    finally:
        server.shutdown()
    return _auth_code["code"]


def _now() -> float:
    import time
    return time.monotonic()


def main() -> int:
    auth_url = (
        "https://accounts.google.com/o/oauth2/auth?"
        + urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        })
    )
    print("=" * 70)
    print("1) Abre esta URL en el navegador donde iniciaste sesion con la")
    print("   cuenta burner:")
    print("=" * 70)
    print(auth_url)
    print()
    print("=" * 70)
    print(f"2) Autoriza el acceso. Google redirigira a {REDIRECT_URI}.")
    print("   Este script captura el codigo automaticamente (no pegues nada).")
    print("=" * 70)
    print("\nEsperando el callback de Google (timeout 5 min)...\n")

    code = _wait_for_code()
    if not code:
        print("No se recibio codigo. Abortando.")
        return 1

    print("Codigo recibido. Intercambiando por refresh_token...")
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}")
        return 2

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        print("Google no devolvio refresh_token. Respuesta completa:")
        print(json.dumps(payload, indent=2))
        print("\nCausa posible: ya autorizaste antes sin prompt=consent.")
        print("Revoca el acceso en https://myaccount.google.com/permissions")
        print("y vuelve a ejecutar el script.")
        return 3

    print()
    print("=" * 70)
    print("REFRESH TOKEN — pon estas 3 variables en Render:")
    print("=" * 70)
    print(f"YOUTUBE_OAUTH_REFRESH_TOKEN={refresh_token}")
    print(f"YOUTUBE_OAUTH_CLIENT_ID={CLIENT_ID}")
    print(f"YOUTUBE_OAUTH_CLIENT_SECRET={CLIENT_SECRET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
