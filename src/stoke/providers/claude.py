"""Claude Code stokes authenticated by CLAUDE_CODE_OAUTH_TOKEN."""

from typing import Any

from ..config import Settings
from ..models import StokeResult, Window
from ..runner import Runner, run_command
from .base import LimitsUnreadable, ProviderError, json_objects, parse_window

ARGV = (
    "claude", "-p", "hi", "--output-format", "stream-json", "--verbose",
    "--no-session-persistence",
)


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

    def __init__(self, settings: Settings, runner: Runner = run_command) -> None:
        self.settings = settings
        self.runner = runner

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
