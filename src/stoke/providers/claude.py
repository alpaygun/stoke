"""Claude Code stokes authenticated by CLAUDE_CODE_OAUTH_TOKEN."""

import json
import logging
from math import ceil
from typing import Any

from .. import clock
from ..config import Settings
from ..httpget import HttpGet, HttpGetError, http_get
from ..models import LimitsSnapshot, ResetCredit, ResetCredits, StokeResult, Window
from ..runner import Runner, run_command
from .base import LimitsUnreadable, ProviderError, ReaderSignedOut, iso_epoch, json_objects, optional_iso_epoch, parse_window, short_error

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
_version_warning_reported = False
_ineligible_reported = False


def _reset_credits(value: Any, now: int) -> ResetCredits | None:
    global _ineligible_reported
    if not isinstance(value, dict):
        return None
    if value.get("eligible") is not True:
        if not _ineligible_reported:
            _ineligible_reported = True
            logger.warning("tool=claude reset credits not eligible (reason=%s); keeping previous list",
                           short_error(str(value.get("ineligible_reason"))))
        return None
    credits = []
    grants = value.get("grants")
    for grant in grants if isinstance(grants, list) else []:
        if not isinstance(grant, dict):
            continue
        count = grant.get("resets_left")
        expires = optional_iso_epoch(grant.get("ends_at"))
        if type(count) is not int or count < 1 or expires is None or expires <= now:
            continue
        clears = grant.get("clears")
        title = "Other reset"
        if isinstance(clears, list):
            if "seven_day" in clears:
                title = "Full reset"
            elif "five_hour" in clears and "seven_day_overage_included" in clears:
                title = "Fable reset"
            elif clears and all(member == "five_hour" for member in clears):
                title = "5-hour reset"
        granted = optional_iso_epoch(grant.get("starts_at"))
        credits.extend(ResetCredit(title, granted, expires) for _ in range(min(count, 20)))
    credits.sort(key=lambda credit: credit.expires_at)
    return ResetCredits(len(credits), credits, 0)


def _usage_window(value: Any, minutes: int) -> Window:
    if not isinstance(value, dict):
        raise LimitsUnreadable("limits unreadable")
    return parse_window({
        "used_percent": value.get("utilization"), "resets_at": iso_epoch(value.get("resets_at")),
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
    reset_closes_window = False
    auth_labels = {"stoke": {"label": "1-year token"}, "read": {"label": "Browser login"}}

    def __init__(self, settings: Settings, runner: Runner = run_command, http: HttpGet = http_get) -> None:
        self.settings = settings
        self.runner = runner
        self.http = http
        self._last_direct_at: int | None = None
        self._login_file_key: tuple[int, int] | None = None
        self._login_expires_at: int | None = None

    def read_login_expires_at(self) -> int | None:
        """Cache only file metadata and expiry, never credentials or tokens."""
        path = self.settings.claude_reader_dir / ".credentials.json"
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            if key == self._login_file_key:
                return self._login_expires_at
            self._login_expires_at = None
            self._login_file_key = key
        except OSError:
            self._login_file_key = None
            self._login_expires_at = None
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            oauth = value.get("claudeAiOauth") if isinstance(value, dict) else None
            expires = oauth.get("refreshTokenExpiresAt") if isinstance(oauth, dict) else None
            if type(expires) is int and expires > 0:
                self._login_expires_at = expires // 1000
        except (OSError, UnicodeError, ValueError, RecursionError):
            pass
        return self._login_expires_at

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        """Prefer the read-only usage endpoint; leave login refresh to the CLI."""
        global _version_warning_reported
        if mode == "cli":
            return await self._read_via_cli()
        if not self.settings.claude_cli_version:
            if not _version_warning_reported:
                _version_warning_reported = True
                logger.warning("direct Claude read needs STOKE_CLAUDE_CLI_VERSION; using CLI")
            return await self._read_via_cli()
        try:
            value = json.loads((self.settings.claude_reader_dir / ".credentials.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ReaderSignedOut("usage reader signed out") from None
        except (OSError, UnicodeError, ValueError, RecursionError):
            return await self._read_via_cli()
        oauth = value.get("claudeAiOauth") if isinstance(value, dict) else None
        if not isinstance(oauth, dict):
            return await self._read_via_cli()
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            raise ReaderSignedOut("usage reader signed out")
        now = clock.now()
        expires = oauth.get("expiresAt")
        if type(expires) is int and expires <= (now + 60) * 1000:
            logger.info("tool=claude direct read: access token expired, using CLI")
            return await self._read_via_cli()
        if self._last_direct_at is not None and now - self._last_direct_at < 600:
            minutes = max(1, ceil((600 - (now - self._last_direct_at)) / 60))
            raise ProviderError(f"Claude usage was read less than 10 minutes ago; try again in {minutes} min")
        headers = {
            "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": f"claude-cli/{self.settings.claude_cli_version} (external, cli)",
        }
        self._last_direct_at = now
        try:
            response = await self.http("https://api.anthropic.com/api/oauth/usage?cedar_ember=1",
                                       headers, min(self.settings.timeout_seconds, 30))
        except HttpGetError as error:
            raise ProviderError(f"Claude usage read failed: {error}") from None
        if response.status == 401:
            logger.info("tool=claude direct read got 401, using CLI")
            return await self._read_via_cli()
        if response.status == 429:
            raise ProviderError("Claude usage endpoint rate limited (HTTP 429)")
        if response.status != 200:
            raise ProviderError(f"Claude usage read failed (HTTP {response.status})")
        try:
            limits = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise LimitsUnreadable("limits unreadable") from None
        if not isinstance(limits, dict):
            raise LimitsUnreadable("limits unreadable")
        weekly = limits.get("seven_day")
        breakdown = limits.get("seven_day_breakdown")
        observed = optional_iso_epoch(breakdown.get("as_of")) if isinstance(breakdown, dict) else None
        return LimitsSnapshot(
            _usage_window(limits.get("five_hour"), 300),
            None if weekly is None else _usage_window(weekly, 10080),
            _reset_credits(limits.get("cedar_ember"), clock.now()), observed,
        )

    async def _read_via_cli(self) -> LimitsSnapshot:
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
                observed = iso_epoch(breakdown.get("as_of"))
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
