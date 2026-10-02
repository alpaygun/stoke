"""Fixture parsing and injected command checks for both official providers."""

import asyncio
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pytest

from stoke.config import Settings
from stoke.models import Window
from stoke.providers import create_providers
from stoke.providers.base import LimitsUnreadable, ProviderError, parse_window, redact
from stoke.providers.claude import ClaudeProvider, parse_claude_stream
from stoke.providers.codex import CodexProvider, find_rollout, parse_codex_rollout, parse_codex_stream
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


def write_rollout(settings: Settings, text: str) -> Path:
    """Write a fixture in the nested official session path."""
    path = settings.codex_home / "sessions" / "2026" / "10" / "02" / f"rollout-2026-10-02T00-00-00-{THREAD}.jsonl"
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
        write_rollout(settings, fixture_text("codex_rollout.jsonl"))
    calls: list[tuple[Sequence[str], Path, float, Mapping[str, str] | None]] = []
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None) -> CommandResult:
        calls.append((argv, cwd, timeout_s, env))
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
@pytest.mark.parametrize("delete_fails", [False, True])
def test_codex_removes_only_its_read_rollout_and_preserves_outcome(
    readable: bool, delete_fails: bool, settings_factory: Callable[..., Settings],
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
        if delete_fails:
            raise PermissionError("private path sk-ant-fixture-secret")
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
    assert unlinked == [path] and path.exists() is delete_fails
    assert other_rollout.read_text(encoding="utf-8") == "unrelated session"
    assert other_file.read_text(encoding="utf-8") == "unrelated data"
    warnings = [record for record in caplog.records if record.name == "stoke.providers.codex"]
    assert [record.getMessage() for record in warnings] == (["rollout delete failed: PermissionError"] if delete_fails else [])
    assert all(record.levelname == "WARNING" and record.exc_info is None for record in warnings)
    assert "private path" not in caplog.text and "sk-ant-" not in caplog.text


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
