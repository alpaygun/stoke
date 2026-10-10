# stoke

Keeps the 5-hour usage window of AI coding CLIs always running.

AI coding subscriptions (Claude Code, Codex CLI) meter usage in 5-hour windows that start with your first message. If no window is running when you sit down, you wait longer for a fresh one. Stoke sends a tiny prompt (`hi`, "a stoke") through the official CLI right after each window ends, so a window is always open, and shows the state on a small panel.

Works with Claude Code and Codex CLI. Runs in Docker (built for a Raspberry Pi, works anywhere).

## How it works

- One loop per tool: stoke, read the window's end time from the CLI's own output, wait until it ends (+90 s), stoke again.
- Each tool can be switched on or off independently from the panel; manual "Stoke now" is always available.
- Only the official, unmodified CLIs talk to the vendors. Stoke never calls vendor APIs with your token.
- A stoke costs a tiny fraction of the weekly limit; it does not raise any limit, it only shifts when windows start.

## Quick start (dry run)

Dry run is on by default and never executes either CLI.

```bash
docker compose up --build
# open http://127.0.0.1:8787
```

## Real mode

1. `cp .env.example .env`, set `STOKE_DRY_RUN=0`.
2. Claude: run `claude setup-token` on any machine with a browser, put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.
3. Codex: `docker compose run --rm stoke codex login --device-auth` and approve the code from any device. Credentials stay in the `codex-home` volume.
4. `docker compose up -d --build`

All settings and defaults are documented in `.env.example`. Never commit `.env`.

## Updating the CLIs

Both CLI versions are pinned in the `Dockerfile` (`CLAUDE_CODE_VERSION`, `CODEX_VERSION`); the CLIs never update themselves. To update, change the version there and rebuild with `docker compose up -d --build`. Each rebuild leaves the old image behind; remove it with `docker image prune -f`.

## Security

The panel has no login of its own and its port is published on `127.0.0.1` only by default (`STOKE_PUBLISH_HOST`; if a tunnel runs in another container, the Docker bridge gateway such as `172.17.0.1` keeps it off the LAN). To reach it remotely, put an access layer in front of it (e.g. Cloudflare Tunnel + Cloudflare Access, or a VPN). Every `POST` requires the `X-Stoke: 1` header so other sites cannot trigger stokes through your browser.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows; use .venv/bin on Linux/macOS
.venv/Scripts/python -m pytest -q
```

Status: early development.
