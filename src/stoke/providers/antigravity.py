"""Run the official Antigravity CLI for fixed prompts and message-free reads."""

import json
import logging
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from .. import clock
from ..config import Settings
from ..models import LimitsSnapshot, QuotaGroup, ScopedLimit, StokeResult, Window
from ..runner import CommandResult, Runner, run_command
from .base import LimitsUnreadable, ProviderError, ReaderSignedOut, StokeSignedOut, StokeUnverified, iso_epoch, json_objects

logger = logging.getLogger(__name__)
STOKE_MODEL = "gemini-3.8-flash-low"
_AUTH_ERROR = "authentication failed or timed out"
_CHILD_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
)
_CONVERSATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _usage_argv(log_file: Path) -> tuple[str, ...]:
    return ("agy", "--output-format", "json", "--log-file", str(log_file), "-p", "/usage")


def _prompt_argv(log_file: Path) -> tuple[str, ...]:
    return (
        "agy", "--model", STOKE_MODEL, "--output-format", "json", "--disable-slash-commands",
        "--log-file", str(log_file), "-p", "hi",
    )


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        last = None
        for payload in json_objects(text):
            last = payload
        return last
    return value if isinstance(value, dict) else None


def _usage_buckets(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise LimitsUnreadable("limits unreadable")
    buckets = {}
    for group in groups:
        entries = group.get("buckets") if isinstance(group, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            key = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(key, str):
                if key in buckets:
                    raise LimitsUnreadable("limits unreadable")
                buckets[key] = entry
    return buckets


def _window(bucket: dict[str, Any], kind: str, observed_at: int) -> tuple[Window, bool]:
    fraction = bucket.get("remaining_fraction")
    if (type(fraction) not in {int, float} or not 0 <= fraction <= 1
            or bucket.get("window") != kind):
        raise LimitsUnreadable("limits unreadable")
    resets_at = iso_epoch(bucket.get("reset_time"))
    minutes = 300 if kind == "5h" else 10080
    synthetic = fraction == 1 and abs(resets_at - (observed_at + minutes * 60)) <= 120
    return Window(max(0.0, min(100.0, (1 - fraction) * 100)), resets_at, minutes), synthetic


def parse_usage(text: str, observed_at: int) -> LimitsSnapshot:
    """Parse quota buckets by id and identify sliding synthetic deadlines."""
    payload = _json_object(text)
    if payload is None:
        raise LimitsUnreadable("limits unreadable")
    if payload.get("status") != "SUCCESS":
        if payload.get("status") == "ERROR" and payload.get("error") == _AUTH_ERROR:
            raise ReaderSignedOut("Antigravity signed out")
        raise ProviderError("Antigravity usage read failed")
    if payload.get("num_turns", 0) != 0:
        logger.warning("tool=antigravity usage read reported model turns")
    buckets = _usage_buckets(payload)
    five_hour, _ = _window(buckets.get("gemini-5h", {}), "5h", observed_at)

    def optional(key: str, kind: str) -> tuple[Window | None, bool]:
        if key in buckets:
            try:
                return _window(buckets[key], kind, observed_at)
            except LimitsUnreadable:
                logger.warning("tool=antigravity invalid usage bucket id=%s", key)
        return None, False

    weekly, weekly_synthetic = optional("gemini-weekly", "weekly")
    argon, argon_synthetic = optional("gemini-argon-wtu-weekly", "weekly")
    third_hour, third_hour_synthetic = optional("3p-5h", "5h")
    third_week, third_week_synthetic = optional("3p-weekly", "weekly")
    scoped = [] if argon is None else [
        ScopedLimit("Argon", argon.used_percent, None if argon_synthetic else argon.resets_at),
    ]
    groups = []
    if third_hour is not None or third_week is not None:
        groups.append(QuotaGroup(
            "third_party", None if third_hour_synthetic else third_hour, None if third_week_synthetic else third_week,
        ))
    return LimitsSnapshot(
        five_hour, weekly, None, observed_at=observed_at, scoped=scoped, groups=groups,
        window_confirmed=five_hour.used_percent > 0 and five_hour.resets_at > observed_at,
        weekly_synthetic=weekly_synthetic,
    )


class AntigravityProvider:
    """Use the CLI's account login without reading its credential file."""

    name = "antigravity"
    label = "Antigravity"
    reset_closes_window = False
    reset_credits: bool = False
    read_modes: bool = False
    auth_labels = {"shared": {"label": "Account login"}}

    def __init__(self, settings: Settings, runner: Runner = run_command) -> None:
        self.settings = settings
        self.runner = runner

    def _signed_in(self) -> bool:
        try:
            metadata = (self.settings.antigravity_dir / "antigravity-oauth-token").stat()
        except OSError:
            return False
        return stat.S_ISREG(metadata.st_mode) and metadata.st_size > 0

    async def _run(self, argv: Callable[[Path], tuple[str, ...]], timeout: float) -> CommandResult:
        workdir = self.settings.data_dir / "workdir-antigravity"
        env = {key: self.settings.process_env[key] for key in _CHILD_ENV if key in self.settings.process_env}
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="stoke-agy-") as directory:
                return await self.runner(argv(Path(directory) / "agy.log"), workdir, timeout, env, stdin=b"")
        except OSError:
            raise ProviderError("Antigravity CLI could not be started") from None

    async def _read_usage(self) -> tuple[LimitsSnapshot, int]:
        if not self._signed_in():
            raise ReaderSignedOut("Antigravity signed out")
        result = await self._run(_usage_argv, min(max(self.settings.timeout_seconds, 75), 90))
        if result.timed_out:
            raise ProviderError("Antigravity CLI timed out")
        try:
            snapshot = parse_usage(result.stdout, clock.now())
        except LimitsUnreadable:
            if result.returncode != 0 and _json_object(result.stdout) is None:
                raise ProviderError("Antigravity usage read failed") from None
            raise
        return snapshot, result.duration_ms

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        """Read usage via the CLI's local slash command in either read mode."""
        snapshot, _ = await self._read_usage()
        return snapshot

    def _cleanup(self, conversation_id: Any, delivered: bool) -> None:
        if not isinstance(conversation_id, str) or _CONVERSATION_ID.fullmatch(conversation_id) is None:
            if delivered:
                logger.warning("antigravity conversation cleanup skipped: missing or invalid id")
            return
        root = self.settings.antigravity_dir
        try:
            if root.parent.is_symlink() or root.is_symlink():
                return
        except Exception as error:
            logger.warning("antigravity conversation cleanup failed: %s", type(error).__name__)
            return
        artifacts = (
            ("conversations", f"{conversation_id}.db"),
            ("conversations", f"{conversation_id}.db-wal"),
            ("conversations", f"{conversation_id}.db-shm"),
            ("conversations", f"{conversation_id}.db-journal"),
            ("presence", f"{conversation_id}.lock"),
            ("brain", conversation_id),
        )
        for directory, name in artifacts:
            parent = root / directory
            path = parent / name
            try:
                if parent.is_symlink():
                    continue
                if directory == "brain" and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            except Exception as error:
                logger.warning("antigravity conversation cleanup failed: %s", type(error).__name__)

    async def stoke(self) -> StokeResult:
        """Send the fixed prompt and confirm its window with a usage read."""
        if not self._signed_in():
            raise StokeSignedOut("Antigravity signed out")
        delivered = False
        conversation_id = None
        try:
            result = await self._run(_prompt_argv, self.settings.timeout_seconds)
            payload = _json_object(result.stdout)
            if payload is not None:
                conversation_id = payload.get("conversation_id")
                if not result.timed_out:
                    status = payload.get("status")
                    delivered = status == "SUCCESS"
                    if status == "ERROR":
                        if payload.get("error") == _AUTH_ERROR:
                            raise StokeSignedOut("Antigravity signed out")
                        raise ProviderError("Antigravity prompt failed")
        finally:
            self._cleanup(conversation_id, delivered)
        return await self._confirm_stoke(delivered, prompt_duration=result.duration_ms)

    async def verify_stoke(self, delivered: bool) -> StokeResult:
        """Reconcile a prior prompt with a usage read alone."""
        return await self._confirm_stoke(delivered)

    async def _confirm_stoke(self, delivered: bool, *, prompt_duration: int | None = None) -> StokeResult:
        try:
            snapshot, read_duration = await self._read_usage()
        except ProviderError:
            raise StokeUnverified("prompt sent; usage unverified", delivered=delivered) from None
        if snapshot.window_confirmed and snapshot.five_hour.resets_at > clock.now():
            return StokeResult(
                snapshot.five_hour, snapshot.weekly, read_duration if prompt_duration is None else prompt_duration,
                scoped=snapshot.scoped, groups=snapshot.groups,
            )
        if delivered:
            raise StokeUnverified("prompt sent; usage unverified", delivered=True)
        raise ProviderError("Antigravity prompt not delivered")
