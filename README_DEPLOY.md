# AISAK Bot — Guía de despliegue (June 2026)

Bot musical para Discord con Lavalink 4.0.8 + youtube-plugin 1.16.0 + fallback
yt-dlp. Diseñado para Render free tier (512MB RAM).

## Qué cambió respecto a la versión anterior

Esta versión resuelve el problema de "metadata carga pero no hay audio". Las
causas eran tres, todas arregladas en esta release:

1. **`youtube-plugin 1.18.1` roto** → bajado a `1.16.0` (última versión que
   todavía soporta `ANDROID_TESTSUITE`, el único cliente que produce audio
   desde IPs de datacenter en junio 2026).
2. **`skipInitialization: true`** impedía que el OAuth se aplicara a los
   clients compatibles → cambiado a `false`.
3. **Sin fallback** → añadido extractor yt-dlp con cliente `tv_embedded` +
   `curl-cffi` TLS impersonation, activado por circuit breaker tras 3 fallos
   consecutivos del youtube-plugin.

## Stack

| Componente            | Versión      | Notas                                    |
|----------------------|--------------|------------------------------------------|
| discord.py           | 2.7.1        | Soporta DAVE (E2EE de voz, marzo 2026)   |
| davey                | 0.1.5        | Lib cripto para DAVE                     |
| wavelink             | 3.5+         | Cliente Lavalink 4.x para Python         |
| Lavalink             | 4.0.8        | Java 17, cabe en 160MB heap              |
| youtube-plugin       | 1.16.0       | ANDROID_TESTSUITE + OAuth                |
| yt-dlp               | ≥2026.3.2    | Fallback con `tv_embedded`               |
| curl-cffi            | ≥0.7.0       | TLS impersonation para evadir bloqueos   |

## Despliegue en Render

1. Sube este repo a GitHub.
2. En Render: New → Web Service → conecta el repo.
3. Plan: **Free**, Region: Oregon, Runtime: Docker.
4. Variables de entorno (mínimas):
   - `DISCORD_TOKEN` — token del bot.
   - `TEST_GUILD_IDS` — IDs de servidores de prueba separados por coma.
   - `YOUTUBE_OAUTH_REFRESH_TOKEN` — ver sección siguiente.

El `render.yaml` ya configura el resto. Si usas `render blueprint`, Render lo
detecta automáticamente.

## Obtener el refresh token de YouTube (importante)

Sin OAuth el bot funciona pero se cae a modo anónimo tras ~30 reproducciones
por hora (YouTube rate-limit). Con OAuth funciona como usuario autenticado.

```bash
python scripts/get_youtube_oauth.py
```

Sigue las instrucciones. Necesitas una cuenta Google burner dedicada (NO uses
tu cuenta personal — YouTube puede banearla por uso automatizado).

## Endpoints de diagnóstico

El bot expone un server aiohttp en `:7860` con:

| Ruta              | Descripción                                              |
|-------------------|----------------------------------------------------------|
| `/health`         | Render healthcheck — siempre devuelve 200 si el bot vive |
| `/status`         | Estado del gateway de Discord                            |
| `/diag`           | Test completo: Lavalink + youtube-plugin + circuit breaker + players |
| `/diag/ytdlp?q=…` | Test directo del fallback yt-dlp                          |
| `/lavalinks`      | Log completo de Lavalink                                 |
| `/botlogs`        | Log completo del bot                                     |

## Cómo diagnosticar "no hay audio"

1. Abre `/diag` en tu navegador:
   ```
   https://<tu-app>.onrender.com/diag
   ```
2. Revisa `lavalink_search.loadType`:
   - `search` con `count > 0` → youtube-plugin funciona. El problema es de
     voz, no de extracción.
   - `error` → youtube-plugin caído. Mira `circuit_breaker.tripped`.
3. Si `circuit_breaker.tripped = true`, el bot está enrutando todo por yt-dlp.
   Verifica que yt-dlp funcione:
   ```
   https://<tu-app>.onrender.com/diag/ytdlp?q=never+gonna+give+you+up
   ```
4. Mira `/lavalinks` para ver warnings del youtube-plugin. Busca:
   - `The web client is not supported` → esperado, lo ignoramos.
   - `OAuth has been enabled without registering any OAuth-compatible clients`
     → significa que `skipInitialization` está en true. Revisa application.yml.
   - `Login required` → ANDROID_TESTSUITE no está en la lista de clients.

## Estructura del proyecto

```
.
├── Dockerfile              # Build de la imagen (Lavalink + bot)
├── application.yml         # Config de Lavalink + youtube-plugin
├── start.sh                # Arranca Lavalink y luego el bot
├── requirements.txt        # Deps Python
├── render.yaml             # Blueprint de Render
├── main.py                 # Entry point del bot
├── config.py               # Settings desde env vars
├── keep_alive.py           # Server aiohttp (health, diag, logs)
├── cogs/
│   ├── music.py            # /play /youtube /playlist /pause /resume /skip
│   ├── queue.py            # /queue /remove /clear /shuffle /repeat /nowplaying
│   ├── controls.py         # /volume /speed /pitch /filter /effectsreset /stop
│   ├── search.py           # /search /lyrics
│   └── help.py             # /help
├── utils/
│   ├── lavalink_manager.py # Núcleo: conexión Lavalink + circuit breaker + yt-dlp fallback
│   ├── music_manager.py    # Estado de reproducción por guild
│   ├── player_controls.py  # Panel de botones interactivo
│   ├── formatters.py       # Embeds
│   ├── models.py           # Track, GuildMusicState, etc.
│   ├── source_router.py    # Clasificación de queries
│   ├── validators.py       # Validación de inputs
│   ├── query_autocomplete.py
│   ├── favorites.py
│   ├── errors.py
│   └── logger.py
├── scripts/
│   └── get_youtube_oauth.py # Helper para obtener refresh token
└── .env.example            # Template de variables de entorno
```

## Límites del Render free tier (512MB)

- Lavalink JVM: 160MB heap + ~40MB metaspace/threads = 200MB
- Bot Python: ~90MB RSS con wavelink + aiohttp
- yt-dlp (cuando se activa): +30-50MB breves
- Sistema + ffmpeg: ~50MB
- Margen para buffers: ~150MB

Si el contenedor muere por OOM (Out Of Memory), sube a starter tier (512MB →
512MB con más CPU y sin spin-down cada 15 min).

## Comandos disponibles (22 slash)

**Reproducción**: `/play` `/playlist` `/youtube` `/pause` `/resume` `/skip`
**Cola**: `/queue` `/remove` `/clear` `/shuffle` `/repeat` `/nowplaying`
**Audio**: `/volume` `/speed` `/pitch` `/filter` `/effectsreset` `/stop`
**Búsqueda**: `/search` `/lyrics`
**Ayuda**: `/help`

## Solución de problemas comunes

### "El bot se conecta al canal pero no reproduce nada"

1. Verifica `YOUTUBE_OAUTH_REFRESH_TOKEN` está configurado en Render.
2. Abre `/diag` y mira `lavalink_search.loadType`.
3. Si es `error`, mira `/lavalinks` para ver el mensaje exacto del plugin.
4. Si `circuit_breaker.tripped = true`, prueba `/diag/ytdlp?q=test` para ver
   si el fallback funciona.

### "Después de ~30 canciones, todo falla"

Es el rate-limit de YouTube sin OAuth. Configura
`YOUTUBE_OAUTH_REFRESH_TOKEN` (ver sección anterior).

### "Lavalink nunca se conecta"

Revisa `/lavalinks`. Si ves `OutOfMemoryError`, baja `-Xmx` en start.sh o
sube a Render starter tier.

### "El panel de controles no se actualiza"

Esperado durante los primeros 5 segundos después de `/play` — el bot está
resolviendo el stream. Si pasan 30s y sigue sin actualizarse, mira el log del
bot en `/botlogs` buscando `DEBUG player.play() RAISED`.

---

**Cross-refs**: [[index.md|AISAK Bot]] · [[log.md|Log del proyecto]] · [[conversacion.md|Conversación]]
