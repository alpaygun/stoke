FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm

# Pinned on purpose: an unpinned install is cached at whatever version the first build saw.
ARG CLAUDE_CODE_VERSION=2.1.296
ARG CODEX_VERSION=0.162.1

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION" "@openai/codex@$CODEX_VERSION" \
    && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 1000 stoke \
    && mkdir -p /data/workdir /home/stoke/.codex \
    && chown -R stoke:stoke /data /home/stoke
COPY src ./src
COPY static ./static

ENV HOME=/home/stoke \
    CODEX_HOME=/home/stoke/.codex \
    DISABLE_AUTOUPDATER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
USER stoke
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; from stoke.config import load_settings; s = load_settings(); urlopen(f'http://127.0.0.1:{s.port}/healthz', timeout=3).read()"
CMD ["python", "-m", "stoke"]
