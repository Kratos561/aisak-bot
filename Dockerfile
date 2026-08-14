# =============================================================================
# AISAK v2 — Dockerfile
# Bot musical para Discord con Lavalink embebido, diseñado para Render free
# tier (512MB RAM). Stack: Python 3.11 + discord.py 2.7 + Lavalink 4.2.2 +
# youtube-plugin 1.18.1 (clientes ANDROID_MUSIC + TVHTML5_SIMPLY + OAuth).
# =============================================================================

FROM python:3.11-slim-bookworm

# ---- Versions (pinned for reproducibility) ---------------------------------
# Lavalink 4.2.2: latest estable con soporte DAVE/E2EE y koe 3.0. Java 17.
# youtube-plugin 1.18.1: latest estable (mayo 2026) con fix critico de OAuth
# aplicado correctamente y soporte TVHTML5_SIMPLY (reemplazo del antiguo
# TVHTML5EMBEDDED). Requiere Lavalink server >= 4.0.7.
ARG LAVALINK_VERSION=4.2.2
ARG YT_PLUGIN_VERSION=1.18.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/user \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Optimización de memoria (MEJORA AGREGADA)
# PYTHONGC: habilita garbage collector más agresivo
# MALLOC_ARENA_MAX: limita arenas de glibc para reducir fragmentación de memoria
ENV PYTHONGC=1 \
    MALLOC_ARENA_MAX=2 \
    JVM_OPTS="-XX:+UseSerialGC -XX:MaxMetaspaceSize=64m"

# ---- System deps -----------------------------------------------------------
# Java 17 (Lavalink), opus/sodium (voz de Discord), ffmpeg (solo emergencias),
# curl + ca-certs (descargas), tzdata (logs con hora correcta).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bash ca-certificates curl tzdata \
      ffmpeg libopus0 libsodium23 \
      openjdk-17-jre-headless && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# ---- Download Lavalink jar + youtube-plugin jar ----------------------------
# The -f flag makes curl fail on 404, which makes the docker build fail too —
# this catches plugin version typos at build time, not at runtime.
RUN mkdir -p /opt/lavalink/plugins && \
    curl -fsSL "https://github.com/lavalink-devs/Lavalink/releases/download/${LAVALINK_VERSION}/Lavalink.jar" \
        -o /opt/lavalink/Lavalink.jar && \
    curl -fsSL "https://github.com/lavalink-devs/youtube-source/releases/download/${YT_PLUGIN_VERSION}/youtube-plugin-${YT_PLUGIN_VERSION}.jar" \
        -o "/opt/lavalink/plugins/youtube-plugin-${YT_PLUGIN_VERSION}.jar" && \
    # Sanity check: both jars must be non-empty.
    test -s /opt/lavalink/Lavalink.jar && \
    test -s "/opt/lavalink/plugins/youtube-plugin-${YT_PLUGIN_VERSION}.jar"

# ---- Non-root user ---------------------------------------------------------
RUN useradd -m -u 1000 user
USER user
WORKDIR $HOME/app

# ---- Python deps (cached layer) --------------------------------------------
COPY --chown=user requirements.txt start.sh ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- App code + Lavalink config --------------------------------------------
COPY --chown=user application.yml /opt/lavalink/application.yml
COPY --chown=user . .
RUN chmod +x start.sh

# ---- Public port (Render expone PORT=10000 por defecto para web services) --
EXPOSE 10000

# ---- Healthcheck: cubre bot + Lavalink ------------------------------------
# Si Lavalink cae, /health devuelve 503 (porque el bot lo reporta).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-10000}/health" || exit 1

# ---- Entrypoint ------------------------------------------------------------
# start.sh arranca Lavalink en background, espera a que responda, y luego
# arranca el bot Python en foreground.
CMD ["./start.sh"]
