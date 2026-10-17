"""Claude Code stokes authenticated by CLAUDE_CODE_OAUTH_TOKEN."""

import logging
from datetime import datetime
from typing import Any

from ..config import Settings
from ..models import LimitsSnapshot, StokeResult, Window
from ..runner import Runner, run_command
from .base import LimitsUnreadable, ProviderError, ReaderSignedOut, json_objects, parse_window

ARGV = (
    "claude", "-p", "hi", "--output-format", "stream-json", "--verbose",
    "--no-session-persistence",
)
READ_ARGV = (
    "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
    "--verbose", "--no-session-persistence",
)
READ_STDIN = b'{"type":"control_request","request_id":"stoke-usage","request":{"subtype":"get_usage","skip_behaviors":true}}\n'
logger = logging.getLogger(__name__)
_credits_reported = False


def _iso_epoch(value: Any) -> int:
    """Accept an offset-aware ISO timestamp with a nonnegative epoch."""
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None or parsed.timestamp() < 0:
            raise ValueError
        return int(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        raise LimitsUnreadable("limits unreadable") from None


def _usage_window(value: Any, minutes: int) -> Window:
    if not isinstance(value, dict):
        raise LimitsUnreadable("limits unreadable")
    return parse_window({
        "used_percent": value.get("utilization"), "resets_at": _iso_epoch(value.get("resets_at")),
        "window_minutes": minutes,
    })


def parse_claude_stream(text: str) -> tuple[Window, Window | None]:
    """Read the last rate event and reject any explicit error result."""
    last: dict[str, Any] | None = None
    for event in json_objects(text):
        if event.get("type") == "result" and event.get("is_error") is True:
            raise ProviderError(str(event.get("result") or "Claude stoke failed"))
        if event.get("type") == "rate_limit_event":
            last = event
    info = last.get("rate_limit_info") if last else None
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    # A top-level resetsAt alone cannot supply the required used_percent.
    if not isinstance(windows, dict):
        raise LimitsUnreadable("limits unreadable")
    five_hour = parse_window(windows.get("five_hour"), claude=True)
    weekly = windows.get("seven_day")
    return five_hour, None if weekly is None else parse_window(weekly, claude=True)


class ClaudeProvider:
    """Run the official Claude CLI in an isolated workdir."""

    name = "claude"
    label = "Claude"
    manual_resets = True
    auth_labels = {"stoke": {"label": "1-year token"}, "read": {"label": "Browser login on the Pi"}}

    def __init__(self, settings: Settings, runner: Runner = run_command) -> None:
        self.settings = settings
        self.runner = runner

    async def read_limits(self) -> LimitsSnapshot:
        """Read usage through the official CLI with an isolated browser login."""
        global _credits_reported
        workdir = self.settings.data_dir / "workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        env = {key: value for key, value in self.settings.process_env.items() if key != "CLAUDE_CODE_OAUTH_TOKEN"}
        env.update(CLAUDE_CONFIG_DIR=str(self.settings.claude_reader_dir), DISABLE_AUTOUPDATER="1")
        try:
            command = await self.runner(READ_ARGV, workdir, self.settings.timeout_seconds, env, stdin=READ_STDIN)
        except TimeoutError:
            raise ProviderError("Claude CLI timed out") from None
        except OSError:
            raise ProviderError("Claude CLI could not be started") from None
        if command.timed_out:
            raise ProviderError("Claude CLI timed out")
        response = next((event["response"] for event in json_objects(command.stdout)
                         if event.get("type") == "control_response" and isinstance(event.get("response"), dict)
                         and event["response"].get("request_id") == "stoke-usage"), None)
        if response is None:
            if command.returncode != 0:
                raise ProviderError(command.stderr or "Claude CLI exited unsuccessfully")
            raise LimitsUnreadable("limits unreadable")
        if response.get("subtype") == "error":
            raise ProviderError(str(response.get("error") or "Claude usage read failed"))
        if command.returncode != 0:
            raise ProviderError(command.stderr or "Claude CLI exited unsuccessfully")
        payload = response.get("response")
        if not isinstance(payload, dict):
            raise LimitsUnreadable("limits unreadable")
        if payload.get("rate_limits_available") is not True:
            raise ReaderSignedOut("usage reader signed out")
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            raise LimitsUnreadable("limits unreadable")
        if limits.get("cedar_ember") is not None and not _credits_reported:
            _credits_reported = True
            logger.warning("tool=claude get_usage now reports reset credits (cedar_ember); stoke does not show them yet")
        observed = None
        breakdown = limits.get("seven_day_breakdown")
        if isinstance(breakdown, dict):
            try:
                observed = _iso_epoch(breakdown.get("as_of"))
            except LimitsUnreadable:
                pass
        weekly = limits.get("seven_day")
        return LimitsSnapshot(
            _usage_window(limits.get("five_hour"), 300),
            None if weekly is None else _usage_window(weekly, 10080), None, observed,
        )

    async def stoke(self) -> StokeResult:
        """Send hi and return the reported windows."""
        workdir = self.settings.data_dir / "workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        env = {**self.settings.process_env, "DISABLE_AUTOUPDATER": "1"}
        try:
            command = await self.runner(ARGV, workdir, self.settings.timeout_seconds, env)
        except OSError:
            raise ProviderError("Claude CLI could not be started") from None
        if command.timed_out:
            raise ProviderError("Claude CLI timed out")
        try:
            five_hour, weekly = parse_claude_stream(command.stdout)
        except LimitsUnreadable:
            if command.returncode != 0:
                raise ProviderError(command.stderr or "Claude CLI exited unsuccessfully") from None
            raise
        if command.returncode != 0:
            raise ProviderError(command.stderr or "Claude CLI exited unsuccessfully")
        return StokeResult(five_hour, weekly, command.duration_ms)
