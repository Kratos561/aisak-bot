FROM python:3.11-slim
ARG BGUTIL_VERSION=1.3.1
ARG NODE_VERSION=22.22.2
ARG DENO_VERSION=2.3.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:/usr/local/bin:$HOME/.deno/bin:$PATH \
    YTDLP_BGUTIL_SERVER_HOME=/home/user/bgutil-ytdlp-pot-provider/server

RUN apt-get update && \
    apt-get install -y --no-install-recommends bash ca-certificates curl ffmpeg git libopus0 libsodium23 unzip xz-utils && \
    rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
      amd64) node_arch='x64'; deno_arch='x86_64-unknown-linux-gnu';; \
      arm64) node_arch='arm64'; deno_arch='aarch64-unknown-linux-gnu';; \
      *) echo "Unsupported architecture: $arch" && exit 1 ;; \
    esac && \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" -o /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && \
    rm -f /tmp/node.tar.xz && \
    node --version && \
    npm --version && \
    curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}.zip" -o /tmp/deno.zip && \
    unzip -d /usr/local /tmp/deno.zip && \
    rm -f /tmp/deno.zip && \
    deno --version

RUN useradd -m -u 1000 user
USER user
WORKDIR $HOME/app

RUN git clone --depth 1 --branch ${BGUTIL_VERSION} https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git $HOME/bgutil-ytdlp-pot-provider && \
    cd $HOME/bgutil-ytdlp-pot-provider/server && \
    npm ci && \
    npx tsc

COPY --chown=user requirements.txt requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY --chown=user . .

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-7860}/health" || exit 1'

CMD ["bash", "-lc", "node \"$HOME/bgutil-ytdlp-pot-provider/server/build/main.js\" & for i in $(seq 1 30); do curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1 && break; sleep 1; done; exec python main.py"]
