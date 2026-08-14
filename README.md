# AISAK v2 — Bot musical para Discord

Bot musical completo y funcional para Discord, optimizado para Render free tier
(512MB) con Lavalink embebido + youtube-plugin + fallback yt-dlp.

**Estado**: AUDITADO y corregido (sesión 024). Versiones reales: Lavalink 4.2.2 +
youtube-plugin 1.18.1 (mayo 2026).


## Mejoras de Optimización (Aplicadas 2026)

### 1. Gestión de Memoria Automática
- **Limpieza automática de estados inactivos**: Cada 30 minutos, el bot limpia automáticamente los `GuildMusicState` de servidores que no han tenido actividad en más de 1 hora.
- **Prevención de memory leaks**: Libera referencias circulares y objetos no utilizados.
- **Endpoint de diagnóstico**: `/memory` muestra estadísticas de uso de memoria en tiempo real.

### 2. Optimización de Sesiones HTTP
- **Reutilización inteligente**: Las sesiones HTTP se reutilizan y cierran correctamente.
- **Limpieza explícita**: Método `cleanup()` para cierre ordenado de recursos.
- **Monitoreo**: `get_memory_usage()` para diagnóstico de uso de memoria.

### 3. Optimización del Contenedor Docker
- **MALLOC_ARENA_MAX=2**: Reduce fragmentación de memoria en glibc.
- **PYTHONGC=1**: Habilita garbage collector más agresivo.
- **JVM optimizado**: SerialGC con Metaspace limitado a 64MB.

### 4. Circuit Breaker Mejorado
- **Detección automática**: Identifica fallos del youtube-plugin.
- **Fallback inteligente**: Usa yt-dlp automáticamente cuando el plugin falla.
- **Recuperación automática**: Restablece el estado después del cooldown.

Estas mejoras permiten que el bot funcione de manera estable en el plan gratuito de Render (512MB RAM) durante períodos prolongados sin degradación de rendimiento.


## Stack

| Componente | Versión |
|------------|---------|
| Lavalink server | 4.2.2 (latest estable, requiere Java 17) |
| youtube-plugin | 1.18.1 (latest estable, mayo 2026) |
| discord.py | 2.7.1 (DAVE/E2EE nativo) |
| wavelink | >=3.5.2, <4.0.0 |
| Python | 3.11 |
| yt-dlp | >=2026.3.2 (fallback) |
| curl-cffi | >=0.7.0 (TLS impersonation para yt-dlp) |

## Setup paso a paso

### 1) Obtén el refresh token de YouTube OAuth (OBLIGATORIO)

**Sin este token el bot solo reproduce URLs directas, no búsquedas por nombre.**

1. Crea una cuenta Google **burner** dedicada (NO uses tu cuenta personal).
2. Crea un proyecto en https://console.cloud.google.com/.
3. APIs & Services → Enable APIs → habilita **YouTube Data API v3**.
4. Credentials → Create OAuth client ID → **Desktop app**.
5. Anota el `Client ID` y `Client Secret`.
6. En tu PC:
   ```bash
   pip install -r requirements.txt
   python scripts/get_oauth.py
   ```
7. El script abre una URL. Ábrela en el navegador con la cuenta burner.
8. Autoriza. Google te da un código. Pégalo en el script.
9. Te devuelve el `refresh_token`. Guárdalo.

> **IMPORTANTE**: El `clientId` y `clientSecret` usados para generar el refresh
> token deben coincidir con los configurados en Render. Si los rotaste en Google
> Cloud Console después, debes regenerar el refresh token.

### 2) Despliega en Render

1. Sube el repo a GitHub.
2. Render: New → Web Service → conecta el repo.
3. Plan Free, Region Oregon, Runtime Docker.
4. Variables de entorno:

| Variable | Valor |
|----------|-------|
| `DISCORD_TOKEN` | Tu token del bot |
| `YOUTUBE_OAUTH_REFRESH_TOKEN` | Token del paso 1 |
| `YOUTUBE_OAUTH_CLIENT_ID` | Client ID del paso 1.4 |
| `YOUTUBE_OAUTH_CLIENT_SECRET` | Client Secret del paso 1.4 |
| `TEST_GUILD_IDS` | IDs de servidores, separados por coma |

### 3) Verifica

Una vez deployado, abre en el navegador:

```
https://<tu-app>.onrender.com/diag
```

Debes ver:
- `lavalink_info.version` = "4.2.2"
- `lavalink_info.plugins[0].name` = "youtube-plugin", version "1.18.1"
- `oauth_configured` = `true`
- `circuit_breaker.tripped` = `false`

Luego prueba desde Discord:
- `/play https://www.youtube.com/watch?v=dQw4w9WgXcQ` → debe sonar
- `/play Milo J` → debe buscar y sonar (requiere OAuth)

## Comandos disponibles (21 slash)

**Reproducción**: `/play` `/pause` `/resume` `/skip` `/stop`
**Cola**: `/queue` `/remove` `/clear` `/shuffle` `/repeat` `/nowplaying`
**Audio**: `/volume` `/speed` `/pitch` `/filter` `/effectsreset`
**Búsqueda**: `/search` `/lyrics`
**Ayuda**: `/help`

Más panel interactivo con botones (⏸️ ▶️ ⏭️ ⏹️ ♾️ 📜) en cada mensaje de
"reproduciendo ahora".

## Arquitectura

```
┌──────────────────────────────────────────────────────────────┐
│ Render free tier (512MB) — Docker container                 │
│                                                              │
│  ┌──────────────────┐    ┌────────────────────────────────┐ │
│  │  Lavalink 4.2.2  │    │  AISAK Bot (Python 3.11)       │ │
│  │  JVM -Xmx128m    │◄───┤  discord.py 2.7 + wavelink 3.5.2│ │
│  │  youtube-plugin  │    │                                │ │
│  │   1.18.1         │    │  aiohttp web server :10000     │ │
│  │  ANDROID_MUSIC + │    │  /health /diag /lavalinks      │ │
│  │  TVHTML5_SIMPLY  │    │                                │ │
│  │  + IOS + WEB     │    │  Circuit breaker: tras N fallos│ │
│  └──────────────────┘    │  del plugin, ruta via yt-dlp   │ │
│                          └────────────────────────────────┘ │
│                                                              │
│  OAuth: clientId+secret+refresh (env vars)                  │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
                   Discord Gateway + voice (DAVE E2EE)
```

## Endpoints de diagnóstico

| Ruta | Descripción |
|------|-------------|
| `/` | Home (JSON status) |
| `/health` | Healthcheck de Render (200 si Lavalink OK, 503 si degraded) |
| `/status` | Estado del runtime |
| `/diag` | Diagnóstico: Lavalink + plugin + circuit breaker + players |
| `/diag/ytdlp?q=...` | Test directo del fallback yt-dlp |
| `/lavalinks` | Log completo de Lavalink |
| `/botlogs` | Notas del bot |

## Solución de problemas

### `/play <texto>` no encuentra nada

Verifica con `/diag`:
- `oauth_configured` debe ser `true`. Si es `false`, falta configurar las 3 vars
  OAuth en Render: `YOUTUBE_OAUTH_REFRESH_TOKEN`, `YOUTUBE_OAUTH_CLIENT_ID`,
  `YOUTUBE_OAUTH_CLIENT_SECRET`.
- `lavalink_search.loadType` debe ser `search`, no `error`.

### `/play <URL>` no funciona

1. Mira `/lavalinks`. Busca errores del plugin.
2. Si el circuit breaker está tripped, el fallback yt-dlp debería estar
   activo. Verifica con `/diag/ytdlp?q=https://www.youtube.com/watch?v=...`.

### Lavalink no conecta (`Cannot connect to host 127.0.0.1:2333`)

El contenedor probablemente se quedó sin memoria. Síntomas:
- Render reinicia el contenedor cada pocos minutos.
- `/lavalinks` muestra "OutOfMemoryError".

Soluciones:
- Bajar `-Xmx128m` en `start.sh` a `-Xmx96m` (margen muy ajustado).
- Subir a Render Starter ($7/mes, 512MB garantizados sin spin-down).

### OAuth token no funciona (400 Bad Request)

Probablemente el refresh token se obtuvo con un client_id diferente al que
usa el plugin. Verifica que las 3 vars OAuth en Render coincidan con la app
de Google Cloud Console. Regenera con `python scripts/get_oauth.py` si es
necesario.

### Bot se queda cargado sin reproducir

Verifica `/diag`:
- Si `lavalink_connected: false` → el proceso Java murió. Mira `/lavalinks`.
- Si `circuit_breaker.tripped: true` → youtube-plugin falló 3 veces, el
  fallback yt-dlp está activo. Espera el cooldown (60s por default).

## Estructura del proyecto

```
.
├── Dockerfile              # Build (Lavalink 4.2.2 + plugin 1.18.1 + bot)
├── application.yml         # Config Lavalink + clients + OAuth
├── start.sh                # Arranca Lavalink, espera, arranca bot
├── requirements.txt        # Deps Python
├── render.yaml             # Blueprint Render
├── main.py                 # Entry point
├── config.py               # Settings (env vars)
├── keep_alive.py           # Server aiohttp (health/diag)
├── cogs/
│   ├── music.py            # /play /pause /resume /skip /stop
│   ├── queue.py            # /queue /remove /clear /shuffle /repeat /nowplaying
│   ├── controls.py         # /volume /speed /pitch /filter /effectsreset
│   ├── search.py           # /search /lyrics
│   └── help.py             # /help
├── utils/
│   ├── lavalink.py         # Lavalink + circuit breaker + yt-dlp fallback
│   ├── music.py            # Estado de reproducción por guild
│   ├── ui.py               # Embeds + panel de botones
│   ├── models.py           # Track, GuildMusicState, RuntimeStatus
│   ├── validators.py       # Validación de inputs
│   ├── query_autocomplete.py
│   ├── errors.py
│   └── logger.py
├── scripts/
│   └── get_oauth.py        # Helper para obtener refresh token OAuth
└── .env.example
```

## Límites del Render free tier (512MB)

| Componente | Memoria |
|-----------|---------|
| Lavalink JVM heap | 128 MB |
| JVM metaspace + threads | ~40 MB |
| Bot Python (RSS) | ~90 MB |
| Sistema + ffmpeg + curl | ~60 MB |
| Margen para bursts | ~190 MB |

Si mueren por OOM, sube a Starter ($7/mes).

## Licencia

MIT.

---

**Cross-refs**: [[index.md|AISAK Bot]] · [[conversacion.md|Conversación]] · [[log.md|Log]] · [[decisiones.md|ADRs]] · [[credentials.md|Credenciales]]