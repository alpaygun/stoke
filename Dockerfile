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

# Pin Antigravity and both release checksums like the npm CLIs.
ARG ANTIGRAVITY_VERSION=1.5.14-5302690847260672
ARG ANTIGRAVITY_SHA512_ARM64=c9b29bba475f63a1a80161ae93ba72b221c7a586d9851e4d0061546704bab66d6b2760b25d716d191724fc7564f3246456ee151e28aa37af48dbcd72bdc6c463
ARG ANTIGRAVITY_SHA512_AMD64=83311d3f354567612dc16c1ae2ea9b503d6f6833aa4e138d39aa229da3f3bb99b0e4d7cca0bce4796b0060d6a51d8113eb7449ed5dc0e7900bf217107ea5bff9
ARG TARGETARCH
COPY scripts/install-antigravity.py /tmp/install-antigravity.py
RUN python /tmp/install-antigravity.py && rm /tmp/install-antigravity.py

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 1000 stoke \
    && mkdir -p /data/workdir /home/stoke/.codex /home/stoke/.gemini \
    && chown -R stoke:stoke /data /home/stoke
COPY pyproject.toml ./
COPY src ./src
COPY static ./static
COPY --chmod=0755 scripts/stoke-login /usr/local/bin/stoke-login

ENV HOME=/home/stoke \
    CODEX_HOME=/home/stoke/.codex \
    DISABLE_AUTOUPDATER=1 \
    AGY_CLI_DISABLE_AUTO_UPDATE=true \
    STOKE_CLAUDE_CLI_VERSION=$CLAUDE_CODE_VERSION \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
USER stoke
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; from stoke.config import load_settings; s = load_settings(); urlopen(f'http://127.0.0.1:{s.port}/healthz', timeout=3).read()"
CMD ["python", "-m", "stoke"]
