#!/bin/bash
# =============================================================================
# start.sh — Arranca Lavalink + bot AISAK en Render free tier (512MB)
# =============================================================================
set -uo pipefail

LOG_PREFIX="[start.sh]"

# -----------------------------------------------------------------------------
# 1) Budget de memoria en contenedor de 512MB:
#      Lavalink JVM heap:    128 MB  (-Xmx128m)
#      JVM metaspace+threads: ~40 MB
#      Bot Python (RSS):     ~90 MB
#      Sistema + ffmpeg:     ~60 MB
#      Slack/bursts:        ~190 MB
#    SerialGC es mejor que G1GC en heaps pequenos (<256MB): menos overhead
#    de bookkeeping, pausas predecibles.
# -----------------------------------------------------------------------------
echo "$LOG_PREFIX Arrancando Lavalink..."
cd /opt/lavalink

# Aplicacion.yml puede tener ${YOUTUBE_OAUTH_REFRESH_TOKEN} — Lavalink lo
# reemplaza por el valor de la env var automaticamente. Si la var esta
# vacia, Lavalink arrancara igualmente en modo anonimo (menos fiable pero
# no se rompe).
java \
  -Xmx128m \
  -Xms64m \
  -XX:+UseSerialGC \
  -XX:+TieredCompilation \
  -XX:TieredStopAtLevel=1 \
  -XX:+ExitOnOutOfMemoryError \
  -Dfile.encoding=UTF-8 \
  -jar Lavalink.jar > /tmp/lavalink.log 2>&1 &

LAVALINK_PID=$!
echo "$LOG_PREFIX Lavalink PID=$LAVALINK_PID, esperando a que escuche en :2333..."

# -----------------------------------------------------------------------------
# 2) Esperar a Lavalink (max 90s — el plugin de YouTube tarda en inicializar
#    OAuth en primer arranque, a veces 60s).
# -----------------------------------------------------------------------------
LAVALINK_READY=0
for i in $(seq 1 90); do
  if ! kill -0 "$LAVALINK_PID" 2>/dev/null; then
    echo "$LOG_PREFIX Lavalink murio durante el boot. Ultimas 30 lineas de log:"
    tail -30 /tmp/lavalink.log
    # NO salimos: dejamos que el bot arranque y reporte el error en /health.
    # Asi Render sigue viendo el puerto 10000 abierto y no mata el contenedor.
    break
  fi
  if curl -fsS "http://127.0.0.1:2333/v4/info" -H "Authorization: youshallnotpass" > /dev/null 2>&1; then
    echo "$LOG_PREFIX Lavalink listo en ${i}s"
    LAVALINK_READY=1
    break
  fi
  sleep 1
done

if [ "$LAVALINK_READY" -ne 1 ]; then
  echo "$LOG_PREFIX WARNING: Lavalink no respondio tras 90s o murio. Arrancando el bot igual; el bot reintentara la conexion."
  echo "$LOG_PREFIX Ultimas 30 lineas de log de Lavalink:"
  tail -30 /tmp/lavalink.log
fi

# -----------------------------------------------------------------------------
# 3) Bot Python en foreground. Si el bot muere, el contenedor muere y Render
#    lo reinicia.
# -----------------------------------------------------------------------------
cd "$HOME/app"
echo "$LOG_PREFIX Arrancando AISAK bot..."
exec python3 -u main.py
