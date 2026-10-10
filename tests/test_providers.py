"""Fixture parsing and injected command checks for both official providers."""

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pytest

from stoke.config import Settings
from stoke.models import Window
from stoke.providers import create_providers
from stoke.providers.base import LimitsUnreadable, ProviderError, parse_window, redact
from stoke.providers.claude import ClaudeProvider, parse_claude_stream
from stoke.providers.codex import (
    ARGV, DELETE_ARGV_PREFIX, CodexProvider, find_rollout, parse_codex_rollout,
    parse_codex_stream, remove_empty_session_dirs,
)
from stoke.providers.dryrun import DryRunProvider
from stoke.runner import CommandResult

THREAD = "01a0fb98-c9b4-7fb2-8f00-e3fa3e33e1f5"


def test_claude_last_event_and_noise(fixture_text: Callable[[str], str]) -> None:
    five, weekly = parse_claude_stream(fixture_text("claude_stream.jsonl"))
    assert five == Window(17.0, 1790942400, None)
    assert weekly == Window(45.0, 1791180000, None)


@pytest.mark.parametrize("text", ["", "noise", '{}', 'null',
    '{"type":"rate_limit_event","rate_limit_info":null}',
    '{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{}}}',
])
def test_claude_missing_limits(text: str) -> None:
    with pytest.raises(LimitsUnreadable):
        parse_claude_stream(text)


def test_claude_fallback_is_unreadable(fixture_text: Callable[[str], str]) -> None:
    with pytest.raises(LimitsUnreadable):
        parse_claude_stream(fixture_text("claude_fallback.jsonl"))


def test_claude_error_result_is_redacted(fixture_text: Callable[[str], str]) -> None:
    with pytest.raises(ProviderError, match=r"Authentication failed: \[REDACTED\]"):
        parse_claude_stream(fixture_text("claude_error.jsonl"))


def test_claude_weekly_optional_and_last_event_wins(fixture_text: Callable[[str], str]) -> None:
    text = '{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0,"resetsAt":1}}}}'
    assert parse_claude_stream(text)[1] is None
    with pytest.raises(LimitsUnreadable):
        parse_claude_stream(fixture_text("claude_stream.jsonl") + '\n{"type":"rate_limit_event"}')


def test_codex_stream_and_rollout(fixture_text: Callable[[str], str]) -> None:
    assert parse_codex_stream(fixture_text("codex_stream.jsonl")) == THREAD
    assert parse_codex_rollout(fixture_text("codex_rollout.jsonl")) == (
        Window(0.0, 1790945490, 300), Window(38.0, 1791245696, 10080),
    )


@pytest.mark.parametrize("fixture", ["codex_failed.jsonl", "codex_error.jsonl"])
def test_codex_failure_events(fixture: str, fixture_text: Callable[[str], str]) -> None:
    with pytest.raises(ProviderError) as error:
        parse_codex_stream(fixture_text(fixture))
    assert "eyJ" not in str(error.value)


@pytest.mark.parametrize("text", ["", "noise", '{}',
    '{"payload":{"rate_limits":null}}', '{"payload":{"rate_limits":{}}}',
])
def test_codex_rollout_missing_limits(text: str) -> None:
    with pytest.raises(LimitsUnreadable):
        parse_codex_rollout(text)


def test_codex_weekly_optional_and_last_object_wins(fixture_text: Callable[[str], str]) -> None:
    text = '{"payload":{"rate_limits":{"primary":{"used_percent":1,"resets_at":1}}}}'
    assert parse_codex_rollout(text) == (Window(1.0, 1, None), None)
    with pytest.raises(LimitsUnreadable):
        parse_codex_rollout(fixture_text("codex_rollout.jsonl") + '\n{"payload":{"rate_limits":{}}}')


@pytest.mark.parametrize("data", [None, {}, {"used_percent": True, "resets_at":1},
    {"used_percent":float("nan"), "resets_at":1}, {"used_percent":101, "resets_at":1},
    {"used_percent":-1, "resets_at":1}, {"used_percent":1, "resets_at":"1"},
    {"used_percent":1, "resets_at":True}, {"used_percent":1, "resets_at":-1},
    {"used_percent":1, "resets_at":1, "window_minutes":0},
])
def test_malformed_window_values_are_unreadable(data: Any) -> None:
    with pytest.raises(LimitsUnreadable):
        parse_window(data)


def write_rollout(settings: Settings, text: str, thread_id: str = THREAD) -> Path:
    """Write a fixture in the nested official session path."""
    path = settings.codex_home / "sessions" / "2026" / "10" / "02" / f"rollout-2026-10-02T00-00-00-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_find_rollout(tmp_path: Path, settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    assert find_rollout(settings.codex_home, THREAD) is None
    path = write_rollout(settings, "")
    assert find_rollout(settings.codex_home, THREAD) == path
    assert find_rollout(settings.codex_home, "different-thread") is None
    assert find_rollout(tmp_path, "../*") is None
    assert parse_codex_stream('{"type":"thread.started","thread_id":"../*"}') is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_provider_argv_cwd_environment_and_duration(
    name: str, settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
) -> None:
    settings = settings_factory(
        STOKE_DRY_RUN="0", CUSTOM_PASSTHROUGH="kept",
        CLAUDE_CODE_OAUTH_TOKEN="synthetic-claude-token",
    )
    if name == "codex":
        path = write_rollout(settings, fixture_text("codex_rollout.jsonl"))
    calls: list[tuple[Sequence[str], Path, float, Mapping[str, str] | None]] = []
    delete_path_states: list[tuple[bool, bool]] = []
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        calls.append((argv, cwd, timeout_s, env))
        if name == "codex" and tuple(argv[:3]) == DELETE_ARGV_PREFIX:
            delete_path_states.append((path.exists(), path.parent.is_dir()))
            return CommandResult(0, f"Deleted session {THREAD}.", "", 99, False)
        return CommandResult(0, fixture_text(f"{name}_stream.jsonl"), "", 42, False)
    provider = (ClaudeProvider if name == "claude" else CodexProvider)(settings, runner)
    result = asyncio.run(provider.stoke())
    argv, cwd, timeout, env = calls[0]
    expected_argv = (
        ("claude", "-p", "hi", "--output-format", "stream-json", "--verbose", "--no-session-persistence")
        if name == "claude" else
        ("codex", "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "hi")
    )
    assert tuple(argv) == expected_argv
    assert "hi" in argv and "--ephemeral" not in argv
    assert cwd == settings.data_dir / "workdir" and cwd.is_dir()
    assert timeout == settings.timeout_seconds and result.duration_ms == 42
    expected = dict(settings.process_env)
    if name == "claude":
        expected["DISABLE_AUTOUPDATER"] = "1"
    else:
        del expected["CLAUDE_CODE_OAUTH_TOKEN"]
        assert env is not None and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env == expected
    assert settings.process_env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-claude-token"
    if name == "codex":
        assert len(calls) == 2
        assert delete_path_states == [(False, True)]
        assert calls[1] == ((*DELETE_ARGV_PREFIX, THREAD), cwd, timeout, env)
        assert not path.exists() and not path.parent.exists()
        assert (settings.codex_home / "sessions").is_dir()
        assert (result.five_hour, result.weekly) == parse_codex_rollout(fixture_text("codex_rollout.jsonl"))
    else:
        assert len(calls) == 1


@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "missing_binary", "cli_error", "limits"])
def test_provider_failures(
    name: str, failure: str, settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
) -> None:
    settings = settings_factory()
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        if failure == "missing_binary":
            raise FileNotFoundError("synthetic sensitive text")
        text = fixture_text("claude_error.jsonl" if name == "claude" else "codex_failed.jsonl") if failure == "cli_error" else ""
        return CommandResult(1 if failure == "exit" else 0, text, "sk-ant-fixture-secret " + "x" * 500, 2, failure == "timeout")
    provider = (ClaudeProvider if name == "claude" else CodexProvider)(settings, runner)
    with pytest.raises(LimitsUnreadable if failure == "limits" else ProviderError) as error:
        asyncio.run(provider.stoke())
    assert len(str(error.value)) <= 200
    assert "sk-ant-" not in str(error.value) and "eyJ" not in str(error.value)
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("rollout", [None, "", '{"payload":{"rate_limits":null}}'])
def test_codex_missing_or_empty_rollout(
    rollout: str | None, settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
) -> None:
    settings = settings_factory()
    if rollout is not None:
        write_rollout(settings, rollout)
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        return CommandResult(0, fixture_text("codex_stream.jsonl"), "", 2, False)
    with pytest.raises(LimitsUnreadable):
        asyncio.run(CodexProvider(settings, runner).stoke())


@pytest.mark.parametrize("readable", [True, False])
@pytest.mark.parametrize("delete_error", [None, PermissionError, RuntimeError])
def test_codex_removes_only_its_read_rollout_and_preserves_outcome(
    readable: bool, delete_error: type[Exception] | None, settings_factory: Callable[..., Settings],
    fixture_text: Callable[[str], str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    text = fixture_text("codex_rollout.jsonl") if readable else '{"payload":{"rate_limits":null}}'
    path = write_rollout(settings, text)
    other_rollout = path.with_name("rollout-2026-10-02T00-00-00-other-thread.jsonl")
    other_rollout.write_text("unrelated session", encoding="utf-8")
    other_file = settings.codex_home / "unrelated.json"
    other_file.write_text("unrelated data", encoding="utf-8")
    original_unlink = Path.unlink
    unlinked: list[Path] = []

    def unlink(target: Path, missing_ok: bool = False) -> None:
        assert target == path and missing_ok is True
        unlinked.append(target)
        if delete_error is not None:
            raise delete_error("private path sk-ant-fixture-secret")
        original_unlink(target, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        return CommandResult(0, fixture_text("codex_stream.jsonl"), "", 42, False)

    if readable:
        result = asyncio.run(CodexProvider(settings, runner).stoke())
        assert result.five_hour == Window(0.0, 1790945490, 300)
        assert result.duration_ms == 42
    else:
        with pytest.raises(LimitsUnreadable, match="limits unreadable"):
            asyncio.run(CodexProvider(settings, runner).stoke())
    assert unlinked == [path] and path.exists() is (delete_error is not None)
    assert other_rollout.read_text(encoding="utf-8") == "unrelated session"
    assert other_file.read_text(encoding="utf-8") == "unrelated data"
    warnings = [record for record in caplog.records if record.name == "stoke.providers.codex"]
    assert [record.getMessage() for record in warnings] == (
        [f"rollout delete failed: {delete_error.__name__}"] if delete_error is not None else []
    )
    assert all(record.levelname == "WARNING" and record.exc_info is None for record in warnings)
    assert "private path" not in caplog.text and "sk-ant-" not in caplog.text


@pytest.mark.parametrize("failure", [
    "turn.failed", "error", "error_before_thread", "exit", "missing", "no_limits",
    "invalid_utf8", "unreadable", "timeout",
])
def test_codex_deletes_session_on_failure(
    failure: str, settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(CLAUDE_CODE_OAUTH_TOKEN="synthetic-claude-token")
    path = write_rollout(settings, fixture_text("codex_rollout.jsonl"))
    if failure == "missing":
        path.unlink()
    elif failure == "no_limits":
        path.write_text('{}', encoding="utf-8")
    elif failure == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif failure == "unreadable":
        original_read = Path.read_text
        def read_text(target: Path, *args: Any, **kwargs: Any) -> str:
            if target == path:
                raise PermissionError("sk-ant-secret private path")
            return original_read(target, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read_text)

    stdout = json.dumps({"type": "thread.started", "thread_id": THREAD})
    if failure in {"turn.failed", "error", "error_before_thread"}:
        event = json.dumps({
            "type": "error" if failure == "error_before_thread" else failure,
            "error": {"message": "original failure sk-ant-secret"},
        })
        stdout = event + "\n" + stdout if failure == "error_before_thread" else stdout + "\n" + event
    calls: list[tuple[Sequence[str], Path, float, Mapping[str, str] | None]] = []
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        calls.append((argv, cwd, timeout_s, env))
        if tuple(argv[:3]) == DELETE_ARGV_PREFIX:
            return CommandResult(0, "", "", 99, False)
        return CommandResult(7 if failure == "exit" else 0, stdout, "original exit", 42, failure == "timeout")

    expected_type = LimitsUnreadable if failure in {
        "missing", "no_limits", "invalid_utf8", "unreadable",
    } else ProviderError
    with pytest.raises(expected_type) as error:
        asyncio.run(CodexProvider(settings, runner).stoke())
    assert type(error.value) is expected_type
    expected_message = (
        "limits unreadable" if expected_type is LimitsUnreadable else
        "Codex CLI timed out" if failure == "timeout" else
        "original exit" if failure == "exit" else "original failure [REDACTED]"
    )
    assert str(error.value) == expected_message
    assert len(calls) == 2 and calls[0][0] == ARGV
    assert calls[1] == ((*DELETE_ARGV_PREFIX, THREAD), *calls[0][1:])
    assert calls[1][3] is not None and "CLAUDE_CODE_OAUTH_TOKEN" not in calls[1][3]
    if failure == "no_limits":
        assert not path.exists()


@pytest.mark.parametrize("thread_id", [
    None, "fixture-thread", THREAD.replace("-", ""), THREAD + "-extra",
    "g" + THREAD[1:], "../*", "sk-ant-secret\n--force",
])
def test_codex_skips_delete_without_canonical_uuid(
    thread_id: str | None, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    stdout = "" if thread_id is None else json.dumps({"type": "thread.started", "thread_id": thread_id})
    calls: list[Sequence[str]] = []
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        calls.append(argv)
        return CommandResult(0, stdout, "", 42, False)
    with pytest.raises(LimitsUnreadable):
        asyncio.run(CodexProvider(settings_factory(), runner).stoke())
    assert calls == [ARGV]
    assert [record.getMessage() for record in caplog.records] == [
        "codex session delete skipped: missing or invalid thread id",
    ]
    assert caplog.records[0].levelname == "WARNING" and "sk-ant-" not in caplog.text


def test_codex_deletes_uppercase_uuid(
    settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
) -> None:
    settings = settings_factory()
    thread_id = THREAD.upper()
    path = write_rollout(settings, fixture_text("codex_rollout.jsonl"), thread_id)
    calls: list[Sequence[str]] = []
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        calls.append(argv)
        return CommandResult(0, json.dumps({"type": "thread.started", "thread_id": thread_id}), "", 42, False)
    result = asyncio.run(CodexProvider(settings, runner).stoke())
    assert calls == [ARGV, (*DELETE_ARGV_PREFIX, thread_id)]
    assert not path.exists() and result.duration_ms == 42


@pytest.mark.parametrize("readable", [True, False])
@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror", "raised_timeout", "unexpected"])
def test_codex_delete_failure_preserves_outcome_and_logs_safely(
    readable: bool, failure: str, settings_factory: Callable[..., Settings],
    fixture_text: Callable[[str], str], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    path = write_rollout(settings, fixture_text("codex_rollout.jsonl") if readable else '{}')
    calls: list[Sequence[str]] = []
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        calls.append(argv)
        if tuple(argv[:3]) == DELETE_ARGV_PREFIX:
            assert not path.exists()
            if failure in {"oserror", "raised_timeout", "unexpected"}:
                exception = {"oserror": OSError, "raised_timeout": TimeoutError, "unexpected": RuntimeError}[failure]
                raise exception("sk-ant-secret private path")
            return CommandResult(7 if failure == "exit" else 0, "sk-ant-secret stdout", "sk-ant-secret\nprivate failure", 99, failure == "timeout")
        return CommandResult(0, fixture_text("codex_stream.jsonl"), "", 42, False)

    if readable:
        result = asyncio.run(CodexProvider(settings, runner).stoke())
        assert (result.five_hour, result.weekly) == parse_codex_rollout(fixture_text("codex_rollout.jsonl"))
        assert result.duration_ms == 42
    else:
        with pytest.raises(LimitsUnreadable, match="limits unreadable"):
            asyncio.run(CodexProvider(settings, runner).stoke())
    assert calls == [ARGV, (*DELETE_ARGV_PREFIX, THREAD)]
    assert not path.parent.exists() and (settings.codex_home / "sessions").is_dir()
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING" and record.exc_info is None
    expected_reason = {
        "exit": "[REDACTED] private failure", "timeout": "timed out", "oserror": "OSError",
        "raised_timeout": "TimeoutError", "unexpected": "RuntimeError",
    }[failure]
    assert record.getMessage() == f"codex session delete failed: {expected_reason}"
    assert "sk-ant-" not in caplog.text and "stdout" not in caplog.text


def test_codex_removes_empty_dirs_and_keeps_files(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    sessions = settings.codex_home / "sessions"
    remove_empty_session_dirs(settings.codex_home)
    assert not sessions.exists()
    empty = sessions / "2025" / "01" / "01"
    empty.mkdir(parents=True)
    occupied = sessions / "2026" / "10" / "02"
    occupied.mkdir(parents=True)
    other_file = occupied / "unrelated.jsonl"
    other_file.write_text("keep", encoding="utf-8")
    remove_empty_session_dirs(settings.codex_home)
    assert not (sessions / "2025").exists()
    assert other_file.read_text(encoding="utf-8") == "keep"
    assert occupied.is_dir() and sessions.is_dir()
    other_file.unlink()
    remove_empty_session_dirs(settings.codex_home)
    assert sessions.is_dir() and list(sessions.iterdir()) == []


def test_codex_directory_errors_are_ignored(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory()
    sessions = settings.codex_home / "sessions"
    blocked, removable = sessions / "blocked", sessions / "removable"
    blocked.mkdir(parents=True)
    removable.mkdir()
    original_rmdir = Path.rmdir
    def rmdir(path: Path) -> None:
        if path == blocked:
            raise PermissionError("sk-ant-secret private path")
        original_rmdir(path)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    remove_empty_session_dirs(settings.codex_home)
    assert blocked.is_dir() and not removable.exists() and sessions.is_dir()


def test_codex_unreadable_sessions_root_is_ignored(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def is_dir(path: Path) -> bool:
        raise PermissionError("sk-ant-secret private path")
    monkeypatch.setattr(Path, "is_dir", is_dir)
    remove_empty_session_dirs(settings_factory().codex_home)


@pytest.mark.parametrize("readable", [True, False])
def test_codex_unexpected_directory_cleanup_error_preserves_outcome(
    readable: bool, settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    write_rollout(settings, fixture_text("codex_rollout.jsonl") if readable else '{}')
    def remove_dirs(codex_home: Path) -> None:
        raise RuntimeError("sk-ant-secret private path")
    monkeypatch.setattr("stoke.providers.codex.remove_empty_session_dirs", remove_dirs)
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        return CommandResult(0, fixture_text("codex_stream.jsonl"), "", 42, False)
    if readable:
        result = asyncio.run(CodexProvider(settings, runner).stoke())
        assert result.five_hour == Window(0.0, 1790945490, 300) and result.duration_ms == 42
    else:
        with pytest.raises(LimitsUnreadable, match="limits unreadable"):
            asyncio.run(CodexProvider(settings, runner).stoke())
    assert [record.getMessage() for record in caplog.records] == [
        "codex session delete failed: RuntimeError",
    ]
    assert "sk-ant-" not in caplog.text and "private path" not in caplog.text


def test_codex_directory_cleanup_skips_symlinks(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory()
    sessions = settings.codex_home / "sessions"
    sessions.mkdir(parents=True)
    link = sessions / "linked"
    walked: list[Path] = []
    removed: list[Path] = []
    def walk(path: Path, *, topdown: bool, followlinks: bool) -> list[tuple[str, list[str], list[str]]]:
        assert not topdown and not followlinks
        walked.append(path)
        return [(str(link), [], []), (str(sessions), ["linked"], [])]
    monkeypatch.setattr("stoke.providers.codex.os.walk", walk)
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == link)
    monkeypatch.setattr(Path, "rmdir", lambda path: removed.append(path))
    remove_empty_session_dirs(settings.codex_home)
    assert walked == [sessions] and removed == []
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == sessions)
    remove_empty_session_dirs(settings.codex_home)
    assert walked == [sessions] and removed == []


def test_nonzero_exit_overrides_readable_limits(
    settings_factory: Callable[..., Settings], fixture_text: Callable[[str], str],
) -> None:
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        return CommandResult(7, fixture_text("claude_stream.jsonl"), "failed", 2, False)
    with pytest.raises(ProviderError, match="failed"):
        asyncio.run(ClaudeProvider(settings_factory(), runner).stoke())


def test_redact_and_truncate_after_masking() -> None:
    assert redact("sk-ant-abc_123-xyz eyJ" + "a" * 25) == "[REDACTED] [REDACTED]"
    assert redact("normal error") == "normal error"
    error = ProviderError("prefix\n" + "sk-ant-" + "a" * 500 + " " + "x" * 500)
    assert len(str(error)) == 200 and "[REDACTED]" in str(error) and "\n" not in str(error)


def test_registry_default_and_enabled_order(settings_factory: Callable[..., Settings]) -> None:
    providers = create_providers(settings_factory(STOKE_TOOLS="codex,claude"))
    assert list(providers) == ["codex", "claude"]
    assert all(isinstance(provider, DryRunProvider) for provider in providers.values())
    assert [provider.label for provider in providers.values()] == ["Codex", "Claude"]
    real = create_providers(settings_factory(STOKE_DRY_RUN="0", STOKE_TOOLS="codex"))
    assert list(real) == ["codex"] and isinstance(real["codex"], CodexProvider)


def test_dry_run_window_and_injected_failure(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    delays: list[float] = []
    async def sleep(seconds: float) -> None:
        delays.append(seconds)
    settings = settings_factory(STOKE_DRY_RUN_WINDOW_SECONDS="120", STOKE_DRY_RUN_FAIL="codex")
    result = asyncio.run(DryRunProvider("claude", "Claude", settings, sleep).stoke())
    assert result.five_hour.resets_at == 1120 and result.weekly is not None
    assert 1 <= delays[0] <= 2
    with pytest.raises(ProviderError, match="dry-run failure"):
        asyncio.run(DryRunProvider("codex", "Codex", settings, sleep).stoke())
