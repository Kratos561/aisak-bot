---
title: AISAK Discord Music Bot
emoji: 🎵
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
---

# AISAK

AISAK es un bot musical para Discord preparado para Hugging Face Spaces con Docker. Mantiene un endpoint HTTP para health checks, gestiona colas por servidor y usa YouTube como fuente unica de reproduccion:

- `auto`: resuelve en YouTube.
- `youtube`: fuerza YouTube con `yt-dlp + yt-dlp-ejs + bgutil` como ruta dedicada.

## Lo que incluye

- Slash commands con `discord.py`.
- Cola por servidor con `play`, `playlist`, `youtube`, `pause`, `resume`, `skip`, `queue`, `remove`, `clear`, `shuffle`, `repeat`, `volume`, `stop`, `search`, `lyrics` y `help`.
- Autocomplete en `/play`, `/search` y `/youtube` para elegir la coincidencia correcta antes de enviar el comando.
- Panel de controles debajo del reproductor con `Pause`, `Skip`, `Stop`, `AutoPlay`, `Dashboard` y `Like`.
- Reproduccion basada en YouTube con `yt-dlp + yt-dlp-ejs + bgutil + FFmpeg`.
- Compatibilidad con voz moderna de Discord mediante `discord.py 2.7.x + DAVE`.
- Resolucion opcional de URLs de Spotify a busquedas reproducibles si configuras `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET`.
- Servidor Flask con `/`, `/health` y `/status` para mantener el Space despierto.
- Dockerfile compatible con Hugging Face Spaces.
- Script para crear o actualizar el monitor de UptimeRobot desde API.

## Estrategia de fuente

- YouTube es la unica fuente oficial de reproduccion.
- `auto` y `/play` resuelven en YouTube.
- Los enlaces que no sean de YouTube se rechazan con un error guiado.
- Si YouTube falla, el bot informa el motivo y no cambia silenciosamente a otra fuente.
- El mensaje de reproduccion trae botones para controlar la cancion sin escribir otro comando.

## Variables necesarias

1. `DISCORD_TOKEN`
2. `TEST_GUILD_IDS` recomendado para pruebas iterativas en un servidor concreto
3. `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET` opcionales
4. `UPTIMEROBOT_API_KEY` y `HF_SPACE_URL` si quieres automatizar el monitor

Variables utiles de reproduccion:

- `YTDLP_YOUTUBE_PLAYER_CLIENTS`: clientes de YouTube preferidos. Recomendado: `mweb,web_safari`
- `YTDLP_YOUTUBE_RETRY_PLAYER_CLIENTS`: clientes de reintento si YouTube exige ruta alternativa. Recomendado: `web_safari,mweb`
- `YTDLP_YOUTUBE_STREAM_ROUTES`: rutas de clientes que se prueban al resolver streams. Recomendado: `web_safari,mweb,mweb+web_safari,web_embedded,tv_simply,ios,android,android_vr`
- `YTDLP_YOUTUBE_COOKIES_B64`: cookies de YouTube en formato Netscape codificadas en base64. Usar solo como secret de Render.
- `YTDLP_YOUTUBE_COOKIES_PATH`: ruta a un archivo de cookies de YouTube ya montado.
- `YTDLP_YOUTUBE_PO_TOKENS`: tokens PO para yt-dlp, separados por coma, por ejemplo `mweb.gvs+...`.
- `YTDLP_YOUTUBE_VISITOR_DATA`: visitor data para casos donde se use PO token sin cookies.
- `YTDLP_JS_RUNTIMES`: runtimes JS permitidos para resolver challenges. Recomendado en Spaces: `node`
- `YTDLP_REMOTE_COMPONENTS`: componentes remotos para `yt-dlp`. Recomendado: `ejs:github`
- `YTDLP_BGUTIL_BASE_URL`: URL del servidor HTTP del proveedor `bgutil`. Recomendado: `http://127.0.0.1:4416`
- `YTDLP_BGUTIL_SERVER_HOME`: ruta del proveedor `bgutil` si quieres habilitar tambien su modo script
- `YTDLP_OPERATION_TIMEOUT`: segundos maximos por operacion bloqueante de `yt-dlp`. Recomendado: `25`
- `PLAY_CANDIDATE_LIMIT`: cuantos candidatos probar al arrancar una busqueda por texto antes de elegir la primera pista reproducible. Recomendado: `6`

## Ejecucion local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py
```

En Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Despliegue en Hugging Face Spaces

1. Crea un Space de tipo `Docker`.
2. Sube el contenido de este directorio.
3. Configura en `Settings > Repository secrets`:
   - `DISCORD_TOKEN`
   - `TEST_GUILD_IDS` con el ID del servidor donde pruebas el bot
   - `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET` si los usaras
4. Espera a que el build termine.
5. Comprueba que `https://tu-space.hf.space/health` responde con `{"health":"ok"}`.

## Despliegue en Render

1. Crea un `Web Service` usando una URL de repositorio Git publico o conecta GitHub/GitLab/Bitbucket.
2. Usa runtime `Docker` para que Render construya este `Dockerfile`.
3. Configura las variables:
   - `DISCORD_TOKEN`
   - `TEST_GUILD_IDS`
   - `PORT` si quieres forzar un puerto distinto al que Render asigna por defecto
4. Usa `/health` como endpoint de health check y para keep-alive externo.
5. Si importas el repo desde GitHub o GitLab, `render.yaml` ya deja preparado un `Web Service` base.

Render inyecta `PORT` automaticamente en Web Services. Este proyecto lo respeta y, si `PORT` no existe, vuelve a `FLASK_PORT`.
La API de Render acepta repositorios `GitHub` o `GitLab` para crear servicios. Si el codigo vive en otro host git, primero hay que espejarlo a uno de esos dos.

## Nota sobre Discord Voice en 2026

Discord exige soporte DAVE/E2EE para participar en canales de voz elegibles desde marzo de 2026. Si el bot entra y sale del canal con cierres `4017`, revisa que el entorno este usando `discord.py 2.7+` y que `davey` este instalado.

## Nota sobre YouTube en 2026

YouTube puede exigir PO Tokens y resolucion de challenges EJS para exponer formatos de audio. Este proyecto mantiene preparada una ruta dedicada con `yt-dlp-ejs` y `bgutil-ytdlp-pot-provider`, usa `mweb` como cliente principal y reintenta con `web_safari` cuando hace falta.

## UptimeRobot

Una vez que conozcas la URL final del despliegue:

```powershell
$env:UPTIMEROBOT_API_KEY="tu_api_key"
$env:TARGET_URL="https://tu-app.onrender.com/health"
python scripts/setup_uptimerobot.py
```

El script busca un monitor existente con ese nombre y URL. Si no existe, crea uno HTTP cada 5 minutos.
