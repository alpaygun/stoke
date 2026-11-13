"""Codex stokes using device-auth credentials in the persistent CODEX_HOME."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..config import Settings
from ..httpget import HttpGet, HttpGetError, http_get
from ..version import stoke_version
from ..models import LimitsSnapshot, ResetCredit, ResetCredits, StokeResult, Window, sanitize_reset_title
from ..runner import RpcError, RpcRunner, Runner, run_command, run_jsonrpc
from .base import LimitsUnreadable, ProviderError, json_objects, optional_iso_epoch, parse_window, plan_name, short_error

ARGV = ("codex", "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "hi")
DELETE_ARGV_PREFIX = ("codex", "delete", "--force")
RPC_ARGV = ("codex", "app-server")
logger = logging.getLogger(__name__)


def parse_rate_limits_response(result: Any) -> LimitsSnapshot:
    """Validate app-server windows and retain only available reset credits."""
    def window(value: Any) -> Window:
        if not isinstance(value, dict):
            raise LimitsUnreadable("limits unreadable")
        return parse_window({
            "used_percent": value.get("usedPercent"), "resets_at": value.get("resetsAt"),
            "window_minutes": value.get("windowDurationMins"),
        })

    limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(limits, dict):
        raise LimitsUnreadable("limits unreadable")
    five_hour = window(limits.get("primary"))
    secondary = limits.get("secondary")
    weekly = None if secondary is None else window(secondary)
    block = result.get("rateLimitResetCredits")
    resets = None
    if isinstance(block, dict) and type(block.get("availableCount")) is int and block["availableCount"] >= 0:
        credits = []
        entries = block.get("credits")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("status") != "available":
                continue
            expires = entry.get("expiresAt")
            if type(expires) is not int or expires < 0:
                continue
            granted = entry.get("grantedAt")
            title = entry.get("title")
            credits.append(ResetCredit(
                sanitize_reset_title(title if isinstance(title, str) else ""),
                granted if type(granted) is int and granted >= 0 else None, expires,
            ))
        credits.sort(key=lambda credit: credit.expires_at)
        resets = ResetCredits(block["availableCount"], credits, 0)
    return LimitsSnapshot(five_hour, weekly, resets)


def _extract_thread_id(text: str) -> str | None:
    """Recover a thread id even when the stream contains failure events."""
    thread_id: str | None = None
    for event in json_objects(text):
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
    return thread_id


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


def remove_empty_session_dirs(codex_home: Path) -> None:
    """Remove empty session directories bottom-up without following symlinks."""
    sessions = codex_home / "sessions"
    try:
        if sessions.is_symlink() or not sessions.is_dir():
            return
    except OSError:
        return
    for directory, _, _ in os.walk(sessions, topdown=False, followlinks=False):
        path = Path(directory)
        if path == sessions:
            continue
        try:
            if not path.is_symlink():
                path.rmdir()
        except OSError:
            pass


class CodexProvider:
    """Run the official Codex CLI and read its session limit data."""

    name = "codex"
    label = "Codex"
    reset_closes_window = True
    auth_labels = {"shared": {"label": "Account login"}}

    def __init__(self, settings: Settings, runner: Runner = run_command, rpc: RpcRunner = run_jsonrpc, http: HttpGet = http_get) -> None:
        self.settings = settings
        self.runner = runner
        self.rpc = rpc
        self.http = http

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        """Read usage directly when the official CLI's login is available."""
        if mode == "cli":
            return await self._read_via_app_server()
        try:
            auth = json.loads((self.settings.codex_home / "auth.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return await self._read_via_app_server()
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        if not isinstance(tokens, dict):
            return await self._read_via_app_server()
        token, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(token, str) or not token or not isinstance(account, str) or not account:
            return await self._read_via_app_server()
        version = stoke_version()
        headers = {
            "Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account,
            "Accept": "application/json", "User-Agent": f"stoke/{version}" if version else "stoke",
        }
        try:
            response = await self.http("https://chatgpt.com/backend-api/wham/usage", headers,
                                       min(self.settings.timeout_seconds, 30))
        except HttpGetError as error:
            raise ProviderError(f"Codex usage read failed: {error}") from None
        if response.status == 401:
            logger.info("tool=codex direct read got 401, using CLI")
            return await self._read_via_app_server()
        if response.status == 429:
            raise ProviderError("Codex usage endpoint rate limited (HTTP 429)")
        if response.status != 200:
            raise ProviderError(f"Codex usage read failed (HTTP {response.status})")
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise LimitsUnreadable("limits unreadable") from None
        limits = payload.get("rate_limit") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            raise LimitsUnreadable("limits unreadable")

        def window(value: Any) -> Window:
            if not isinstance(value, dict):
                raise LimitsUnreadable("limits unreadable")
            seconds = value.get("limit_window_seconds")
            return parse_window({
                "used_percent": value.get("used_percent"), "resets_at": value.get("reset_at"),
                "window_minutes": seconds // 60 if type(seconds) is int and seconds > 0 else None,
            })

        five_hour = window(limits.get("primary_window"))
        secondary = limits.get("secondary_window")
        weekly = None if secondary is None else window(secondary)
        resets = await self._read_direct_credits(headers)
        return LimitsSnapshot(five_hour, weekly, resets, plan_name=plan_name(payload.get("plan_type")))

    async def _read_direct_credits(self, headers: Mapping[str, str]) -> ResetCredits | None:
        """A credits failure must never discard a successful usage read."""
        try:
            response = await self.http("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
                                       headers, min(self.settings.timeout_seconds, 30))
            if response.status != 200:
                logger.warning("tool=codex reset credits read failed (HTTP %s)", response.status)
                return None
            block = json.loads(response.body)
            if not isinstance(block, dict) or type(block.get("available_count")) is not int or block["available_count"] < 0:
                logger.warning("tool=codex reset credits read failed: invalid data")
                return None
            credits = []
            entries = block.get("credits")
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("status") != "available":
                    continue
                expires = optional_iso_epoch(entry.get("expires_at"))
                if expires is None:
                    continue
                title = entry.get("title")
                credits.append(ResetCredit(
                    sanitize_reset_title(title if isinstance(title, str) else ""),
                    optional_iso_epoch(entry.get("granted_at")), expires,
                ))
            credits.sort(key=lambda credit: credit.expires_at)
            return ResetCredits(block["available_count"], credits, 0)
        except (HttpGetError, ValueError, UnicodeError, RecursionError) as error:
            logger.warning("tool=codex reset credits read failed: %s", type(error).__name__)
            return None

    async def _read_via_app_server(self) -> LimitsSnapshot:
        """Read official usage without creating a thread or spending a reset."""
        workdir = self.settings.data_dir / "workdir"
        workdir.mkdir(parents=True, exist_ok=True)
        env = {key: value for key, value in self.settings.process_env.items() if key != "CLAUDE_CODE_OAUTH_TOKEN"}
        try:
            response = await self.rpc(
                RPC_ARGV, workdir, min(self.settings.timeout_seconds, 30), env,
                {"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "stoke", "title": "stoke", "version": "1"}}},
                [{"method": "initialized"}], {"method": "account/rateLimits/read", "id": 1},
            )
        except RpcError as error:
            raise ProviderError(f"Codex usage read failed: {error}") from None
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else None
            raise ProviderError(str(message or "Codex usage read failed"))
        return parse_rate_limits_response(response.get("result"))

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
        cleanup_thread_id = _extract_thread_id(command.stdout)
        try:
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
                    except Exception as error:
                        logger.warning("rollout delete failed: %s", type(error).__name__)
            except (OSError, UnicodeError):
                raise LimitsUnreadable("limits unreadable") from None
            return StokeResult(five_hour, weekly, command.duration_ms)
        finally:
            await self._cleanup(cleanup_thread_id, workdir, env)

    async def _cleanup(self, thread_id: str | None, workdir: Path, env: Mapping[str, str]) -> None:
        """Delete the official session without changing the stoke outcome."""
        try:
            try:
                if thread_id is None or not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    thread_id, re.IGNORECASE,
                ):
                    logger.warning("codex session delete skipped: missing or invalid thread id")
                    return
                command = await self.runner(
                    (*DELETE_ARGV_PREFIX, thread_id), workdir, self.settings.timeout_seconds, env
                )
                if command.timed_out:
                    logger.warning("codex session delete failed: timed out")
                elif command.returncode != 0:
                    logger.warning("codex session delete failed: %s", short_error(
                        command.stderr or f"exit code {command.returncode}"
                    ))
            finally:
                remove_empty_session_dirs(self.settings.codex_home)
        except Exception as error:
            logger.warning("codex session delete failed: %s", type(error).__name__)
