"""Codex stokes using device-auth credentials in the persistent CODEX_HOME."""

import logging
import re
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import StokeResult, Window
from ..runner import Runner, run_command
from .base import LimitsUnreadable, ProviderError, json_objects, parse_window

ARGV = ("codex", "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "hi")
logger = logging.getLogger(__name__)


def parse_codex_stream(text: str) -> str | None:
    """Return a safe thread id or raise for a failed/error event."""
    thread_id: str | None = None
    for event in json_objects(text):
        if event.get("type") in {"turn.failed", "error"}:
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise ProviderError(str(message or event.get("message") or "Codex stoke failed"))
        if event.get("type") == "thread.started":
            candidate = event.get("thread_id")
            if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
                thread_id = candidate
    return thread_id


def find_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """Locate a thread's rollout without permitting path or glob injection."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        return None
    matches = sorted((codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"))
    return next((path for path in reversed(matches) if path.is_file()), None)


def parse_codex_rollout(text: str) -> tuple[Window, Window | None]:
    """Read the last non-null rate_limits object, regardless of outer type."""
    last: dict[str, Any] | None = None
    for event in json_objects(text):
        payload = event.get("payload")
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if isinstance(limits, dict):
            last = limits
    if last is None:
        raise LimitsUnreadable("limits unreadable")
    five_hour = parse_window(last.get("primary"))
    weekly = last.get("secondary")
    return five_hour, None if weekly is None else parse_window(weekly)


class CodexProvider:
    """Run the official Codex CLI and read its session limit data."""

    name = "codex"
    label = "Codex"

    def __init__(self, settings: Settings, runner: Runner = run_command) -> None:
        self.settings = settings
        self.runner = runner

    async def stoke(self) -> StokeResult:
        """Send hi, then parse the rollout belonging to the new thread."""
        workdir = self.settings.data_dir / "workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        env = {
            key: value for key, value in self.settings.process_env.items()
            if key != "CLAUDE_CODE_OAUTH_TOKEN"
        }
        try:
            command = await self.runner(
                ARGV, workdir, self.settings.timeout_seconds, env
            )
        except OSError:
            raise ProviderError("Codex CLI could not be started") from None
        if command.timed_out:
            raise ProviderError("Codex CLI timed out")
        thread_id = parse_codex_stream(command.stdout)
        if command.returncode != 0:
            raise ProviderError(command.stderr or "Codex CLI exited unsuccessfully")
        if thread_id is None:
            raise LimitsUnreadable("limits unreadable")
        try:
            path = find_rollout(self.settings.codex_home, thread_id)
            if path is None:
                raise LimitsUnreadable("limits unreadable")
            text = path.read_text(encoding="utf-8")
            try:
                five_hour, weekly = parse_codex_rollout(text)
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning("rollout delete failed: %s", type(error).__name__)
        except (OSError, UnicodeError):
            raise LimitsUnreadable("limits unreadable") from None
        return StokeResult(five_hour, weekly, command.duration_ms)
