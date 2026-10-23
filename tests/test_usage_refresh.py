"""Claude usage reads, opt-in refresh, manual credits and live sign-in data."""

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx
import pytest

from conftest import FakeProvider, FakeReader
from stoke.api import create_app
from stoke.config import Settings, load_settings
from stoke.models import AutoRefresh, LimitsSnapshot, ManualReset, Plan, PlanOutcome, ReadRecord, ResetCredit, ResetCredits, StokeRecord, Window, serialize_tool
from stoke.providers import create_providers
from stoke.providers.base import LimitsUnreadable, ProviderError, ReaderSignedOut
from stoke.providers.claude import ClaudeProvider
from stoke.providers.codex import CodexProvider
from stoke.runner import CommandResult
from stoke.scheduler import AlreadyRunning, ManualResetsUnsupported, RefreshUnsupported, Scheduler
from stoke.store import load, save

HEADERS = {"X-Stoke": "1"}
ISO = "2026-10-17T08:59:59.694853+00:00"
EPOCH = int(datetime.fromisoformat(ISO).timestamp())


def usage(**overrides: Any) -> dict[str, Any]:
    return {
        "five_hour": {"utilization": 17.5, "resets_at": ISO},
        "seven_day": {"utilization": 45, "resets_at": ISO},
        "seven_day_breakdown": {"as_of": ISO}, "cedar_ember": None, **overrides,
    }


def response(limits: Any = None, **overrides: Any) -> str:
    return json.dumps({"type": "control_response", "response": {
        "request_id": "stoke-usage", "subtype": "success",
        "response": {"rate_limits_available": True, "rate_limits": usage() if limits is None else limits},
        **overrides,
    }})


def reader(settings: Settings, stdout: str = "", code: int = 0, stderr: str = "", timed_out: bool = False) -> ClaudeProvider:
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None,
                     *, stdin: bytes | None = None) -> CommandResult:
        return CommandResult(code, stdout, stderr, 1, timed_out)
    return ClaudeProvider(settings, runner)


def scheduler_for(settings: Settings, provider: FakeProvider | None = None) -> Scheduler:
    actual = provider if provider is not None else FakeReader()
    providers = {actual.name: actual}
    return Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)


def test_claude_exact_read_command_and_isolated_environment(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(CLAUDE_CODE_OAUTH_TOKEN="synthetic-token", CUSTOM="kept", STOKE_CLAUDE_READER_DIR="reader-dir")
    async def runner(argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str] | None = None,
                     *, stdin: bytes | None = None) -> CommandResult:
        assert tuple(argv) == ("claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--no-session-persistence")
        assert stdin == b'{"type":"control_request","request_id":"stoke-usage","request":{"subtype":"get_usage","skip_behaviors":true}}\n'
        assert cwd == settings.data_dir / "workdir" and cwd.is_dir()
        assert timeout_s == settings.timeout_seconds
        assert env == {k: v for k, v in {**settings.process_env, "CLAUDE_CONFIG_DIR": "reader-dir", "DISABLE_AUTOUPDATER": "1"}.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in env
        noise = 'junk\n[]\n{"type":"control_response","response":null}\n'
        other = response(request_id="different")
        return CommandResult(0, noise + other + "\n" + response() + "\n" + response(usage(five_hour=None)), "", 1, False)
    snapshot = asyncio.run(ClaudeProvider(settings, runner).read_limits())
    assert snapshot == LimitsSnapshot(Window(17.5, EPOCH, 300), Window(45, EPOCH, 10080), None, EPOCH)
    assert settings.process_env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-token"
    assert "CLAUDE_CONFIG_DIR" not in settings.process_env


@pytest.mark.parametrize("weekly", ["missing", None])
@pytest.mark.parametrize("as_of", ["missing", None, "bad", "2026-10-17T08:59:59", ISO])
def test_claude_optional_windows_and_observation(weekly: Any, as_of: Any, settings_factory: Callable[..., Settings]) -> None:
    limits = usage(seven_day=None, seven_day_breakdown=None if as_of == "missing" else {"as_of": as_of})
    if weekly == "missing":
        del limits["seven_day"]
    snapshot = asyncio.run(reader(settings_factory(), response(limits)).read_limits())
    assert snapshot.weekly is None and snapshot.observed_at == (EPOCH if as_of == ISO else None)


@pytest.mark.parametrize("percent", [0, 1, 100])
def test_claude_usage_is_already_percent(percent: int, settings_factory: Callable[..., Settings]) -> None:
    limits = usage(five_hour={"utilization": percent, "resets_at": "2026-10-17T11:59:59.694853+03:00"})
    snapshot = asyncio.run(reader(settings_factory(), response(limits)).read_limits())
    assert snapshot.five_hour == Window(percent, EPOCH, 300)


@pytest.mark.parametrize("field,value", [
    ("utilization", True), ("utilization", "1"), ("utilization", -1), ("utilization", 101),
    ("utilization", float("inf")), ("utilization", float("nan")),
    ("resets_at", None), ("resets_at", 1), ("resets_at", "bad"),
    ("resets_at", "2026-10-17T08:00:00"), ("resets_at", "1960-01-01T00:00:00+00:00"),
])
@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
def test_claude_invalid_usage_windows(field: str, value: Any, window: str, settings_factory: Callable[..., Settings]) -> None:
    limits = usage()
    limits[window][field] = value
    with pytest.raises(LimitsUnreadable, match="^limits unreadable$"):
        asyncio.run(reader(settings_factory(), response(limits)).read_limits())


@pytest.mark.parametrize("limits", [{}, {"five_hour": None}, {"five_hour": []}, []])
def test_claude_required_window(limits: Any, settings_factory: Callable[..., Settings]) -> None:
    with pytest.raises(LimitsUnreadable):
        asyncio.run(reader(settings_factory(), response(limits)).read_limits())


@pytest.mark.parametrize("available", [False, None, 1, "true"])
def test_claude_reader_signed_out(available: Any, settings_factory: Callable[..., Settings]) -> None:
    text = response(response={"rate_limits_available": available})
    with pytest.raises(ReaderSignedOut, match="^usage reader signed out$"):
        asyncio.run(reader(settings_factory(), text).read_limits())


@pytest.mark.parametrize("error", ["denied sk-ant-test-secret", ""])
def test_claude_error_response(error: str, settings_factory: Callable[..., Settings]) -> None:
    with pytest.raises(ProviderError) as caught:
        asyncio.run(reader(settings_factory(), response(subtype="error", error=error)).read_limits())
    assert str(caught.value) == ("denied [REDACTED]" if error else "Claude usage read failed")


@pytest.mark.parametrize("text,code,stderr,timeout,expected", [
    ("", 0, "", False, "limits unreadable"),
    (response(request_id="other"), 0, "", False, "limits unreadable"),
    ("", 1, "", False, "Claude CLI exited unsuccessfully"),
    ("", 1, "denied sk-ant-secret", False, "denied [REDACTED]"),
    (response(), 1, "exit", False, "exit"),
    (response(), 0, "", True, "Claude CLI timed out"),
])
def test_claude_read_failure_paths(text: str, code: int, stderr: str, timeout: bool, expected: str, settings_factory: Callable[..., Settings]) -> None:
    with pytest.raises(ProviderError, match=re.escape(expected)):
        asyncio.run(reader(settings_factory(), text, code, stderr, timeout).read_limits())


@pytest.mark.parametrize("exception,expected", [(OSError, "could not be started"), (TimeoutError, "timed out")])
def test_claude_read_start_and_timeout_errors(exception: type[Exception], expected: str, settings_factory: Callable[..., Settings]) -> None:
    async def runner(*args: Any, stdin: bytes | None = None, **kwargs: Any) -> CommandResult:
        raise exception("private text")
    with pytest.raises(ProviderError, match=f"^Claude CLI {expected}$"):
        asyncio.run(ClaudeProvider(settings_factory(), runner).read_limits())


def test_claude_vendor_credits_logged_once_without_contents(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr("stoke.providers.claude._credits_reported", False)
    text = response(usage(cedar_ember={"private": "do not show"}))
    for _ in range(2):
        snapshot = asyncio.run(reader(settings_factory(), text).read_limits())
        assert snapshot.resets is None
    assert [r.getMessage() for r in caplog.records] == ["tool=claude get_usage now reports reset credits (cedar_ember); stoke does not show them yet"]
    assert "do not show" not in caplog.text


@pytest.mark.parametrize("value", ["2026-1-01", "20261017", "2026-02-30", "bad", "2026-10-17\n", " 2026-10-17", "2026-10-17T00:00:00"])
def test_invalid_token_expiry(value: str) -> None:
    with pytest.raises(ValueError, match="^STOKE_CLAUDE_TOKEN_EXPIRES must be YYYY-MM-DD$"):
        load_settings({"STOKE_CLAUDE_TOKEN_EXPIRES": value})


def test_token_expiry_local_midnight_and_removed_global_refresh() -> None:
    settings = load_settings({"STOKE_CLAUDE_TOKEN_EXPIRES": "2026-10-17", "STOKE_TIMEZONE": "America/New_York", "STOKE_REFRESH_SECONDS": "invalid"})
    assert settings.claude_token_expires == int(datetime(2026, 10, 17, tzinfo=ZoneInfo("America/New_York")).timestamp())
    assert not hasattr(settings, "refresh_seconds")
    assert load_settings({"STOKE_CLAUDE_TOKEN_EXPIRES": ""}).claude_token_expires is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_new_state_roundtrip_and_v051_compatibility(name: str, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture) -> None:
    scheduler = scheduler_for(settings_factory(), FakeProvider(name))
    state = scheduler.states[name]
    state.five_hour, state.weekly, state.limits_at = Window(42, 2000, 300), Window(13, 40000, 10080), 1000
    state.next_stoke_at = 2090
    state.auto_stoke, state.consecutive_failures, state.stale_resets = True, 2, 1
    state.warning = "limits_unreadable"
    state.last_stoke = StokeRecord(1000, "manual", False, "limits unreadable", None, 10)
    state.plan = Plan("07:00", "daily", True, 9000, None)
    state.plan_outcome = PlanOutcome(1000, "missed", 1100)
    state.resets = ResetCredits(1, [ResetCredit("Full reset", 1000, 20000)], 1200)
    state.auto_refresh = AutoRefresh(True, 90)
    state.last_read = ReadRecord(1000, False, "usage reader signed out", True)
    state.manual_resets = [ManualReset("abcdef012345", "Full reset", 5000, 1000)]
    save(scheduler.path, scheduler.states)
    assert load(scheduler.path, scheduler.settings, scheduler.providers) == scheduler.states
    data = json.loads(scheduler.path.read_text(encoding="utf-8"))
    for item in data.values():
        for key in ("auto_refresh", "last_read", "manual_resets"):
            del item[key]
    scheduler.path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    restored = load(scheduler.path, scheduler.settings, scheduler.providers)[name]
    assert restored == replace(state, auto_refresh=AutoRefresh(), last_read=None, manual_resets=[])
    assert "using defaults" not in caplog.text


@pytest.mark.parametrize("key,value", [
    ("auto_refresh", None), ("auto_refresh", {"enabled": 1}), ("auto_refresh", {"seconds": True}),
    ("auto_refresh", {"seconds": 59}), ("auto_refresh", {"seconds": 86401}),
    ("last_read", []), ("last_read", {"at": 1, "ok": True, "error": [], "signed_out": False}),
    ("manual_resets", None), ("manual_resets", [{}]),
    ("manual_resets", [{"id": "abcdef012345", "title": "bad", "expires_at": 2, "added_at": 1}]),
    ("manual_resets", [{"id": "ABCDEF012345", "title": "Full reset", "expires_at": 2, "added_at": 1}]),
    ("manual_resets", [{"id": "abcdef012345", "title": "Full reset", "expires_at": True, "added_at": 1}]),
    ("manual_resets", [{"id": "abcdef012345", "title": "Full reset", "expires_at": 2, "added_at": -1}]),
])
def test_invalid_new_state_is_corrupt(key: str, value: Any, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture) -> None:
    scheduler = scheduler_for(settings_factory())
    scheduler.states["codex"].five_hour = Window(42, 2000, 300)
    save(scheduler.path, scheduler.states)
    data = json.loads(scheduler.path.read_text(encoding="utf-8"))
    data["codex"][key] = value
    scheduler.path.write_text(json.dumps(data), encoding="utf-8")
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"].five_hour is None
    assert "using defaults" in caplog.text


@pytest.mark.parametrize("observed,expected", [(800, 900), (900, 900), (950, 950), (2000, 1000), (None, 1000)])
def test_observation_time_and_read_record(observed: int | None, expected: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    provider = FakeReader()
    provider.read_result = replace(provider.read_result, observed_at=observed)
    scheduler = scheduler_for(settings_factory(), provider)
    state = scheduler.states["codex"]
    state.five_hour, state.weekly, state.limits_at = Window(42, 2000, 300), Window(13, 40000, 10080), 900
    before = deepcopy(state)
    asyncio.run(scheduler.refresh_limits("codex"))
    assert state.last_read == ReadRecord(1000, True, None, False)
    assert state.limits_at == expected
    assert state.five_hour == Window(42 if observed == 800 else 8, 2000, 300)
    assert state.weekly == (before.weekly if observed == 800 else provider.read_result.weekly)
    assert state.resets == (None if observed == 800 else replace(provider.read_result.resets, read_at=1000))


@pytest.mark.parametrize("failure,error,signed_out", [
    (ReaderSignedOut("usage reader signed out"), "usage reader signed out", True),
    (ReaderSignedOut("different text"), "usage reader signed out", True),
    (ProviderError("failed sk-ant-secret"), "failed [REDACTED]", False),
    (RuntimeError("private text"), "unexpected provider error", False),
])
def test_failed_read_record_persisted(failure: Exception, error: str, signed_out: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    provider = FakeReader()
    provider.read_result = failure
    scheduler = scheduler_for(settings_factory(), provider)
    with pytest.raises(ProviderError):
        asyncio.run(scheduler.refresh_limits("codex"))
    assert scheduler.states["codex"].last_read == ReadRecord(1000, False, error, signed_out)
    assert load(scheduler.path, scheduler.settings, scheduler.providers) == scheduler.states


@pytest.mark.parametrize("enabled,seconds", [(1, 300), (None, 300), (True, True), (True, 60.0), (False, 59), (True, 86401)])
def test_auto_refresh_strict_validation(enabled: Any, seconds: Any, settings_factory: Callable[..., Settings]) -> None:
    scheduler = scheduler_for(settings_factory())
    with pytest.raises(ValueError):
        scheduler.set_auto_refresh("codex", enabled, seconds)
    unsupported = scheduler_for(settings_factory(), FakeProvider())
    with pytest.raises(RefreshUnsupported):
        unsupported.set_auto_refresh("claude", True, 300)


def test_refresh_loop_opt_in_interval_change_disable_and_independence(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        scheduler = scheduler_for(settings_factory())
        provider = scheduler.providers["codex"]
        waits: asyncio.Queue[tuple[float, asyncio.Future[bool]]] = asyncio.Queue()
        original = asyncio.wait_for
        async def wait_for(awaitable: Any, timeout: float) -> Any:
            awaitable.close()
            future = asyncio.get_running_loop().create_future()
            waits.put_nowait((timeout, future))
            return await future
        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", wait_for)
        await scheduler.start()
        try:
            await asyncio.sleep(0)
            assert provider.read_calls == 0 and waits.empty()
            scheduler.set_auto_refresh("codex", True, 300)
            seconds, gate = await original(waits.get(), 0.5)
            assert seconds == 300 and provider.read_calls == 1
            gate.set_exception(TimeoutError())
            seconds, gate = await original(waits.get(), 0.5)
            assert seconds == 300 and provider.read_calls == 2
            scheduler.set_auto_refresh("codex", True, 90)
            gate.set_result(True)
            seconds, gate = await original(waits.get(), 0.5)
            assert seconds == 90 and provider.read_calls == 3
            scheduler.set_auto_refresh("codex", False, 90)
            gate.set_result(True)
            await asyncio.sleep(0)
            assert provider.read_calls == 3 and waits.empty()
            await scheduler.refresh_limits("codex")
            assert provider.read_calls == 4
            assert not load(scheduler.path, scheduler.settings, scheduler.providers)["codex"].auto_refresh.enabled
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ProviderError("failed"), RuntimeError("private text")])
def test_refresh_loop_backoff_cap_reset_and_busy(failure: Exception, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        scheduler = scheduler_for(settings_factory())
        outcomes = [failure] * 8 + [None, AlreadyRunning(), None]
        waits: asyncio.Queue[tuple[float, asyncio.Future[bool]]] = asyncio.Queue()
        original = asyncio.wait_for
        async def read(name: str) -> Any:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome
            return scheduler.states[name]
        async def wait_for(awaitable: Any, timeout: float) -> bool:
            awaitable.close()
            future = asyncio.get_running_loop().create_future()
            waits.put_nowait((timeout, future))
            return await future
        monkeypatch.setattr(scheduler, "refresh_limits", read)
        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", wait_for)
        scheduler.set_auto_refresh("codex", True, 60)
        await scheduler.start()
        try:
            for index, expected in enumerate([120, 240, 480, 960, 1920, 3600, 3600, 3600, 60, 10, 60]):
                seconds, gate = await original(waits.get(), 0.5)
                assert seconds == expected
                if index == 0:
                    scheduler.set_auto_refresh("codex", True, 60)
                    gate.set_result(True)
                elif index < 10:
                    gate.set_exception(TimeoutError())
            assert not outcomes
        finally:
            await scheduler.stop()
    asyncio.run(scenario())
    assert "private text" not in caplog.text


def test_refresh_setting_wakes_actual_wait_and_survives_change_during_read(settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        provider = FakeReader()
        provider.read_release = asyncio.Event()
        scheduler = scheduler_for(settings_factory(), provider)
        scheduler.set_auto_refresh("codex", True, 86400)
        await scheduler.start()
        try:
            await asyncio.wait_for(provider.read_started.wait(), 0.5)
            scheduler.set_auto_refresh("codex", True, 90)
            provider.read_started.clear()
            provider.read_release.set()
            await asyncio.wait_for(provider.read_started.wait(), 0.5)
            assert provider.read_calls == 2
            provider.read_started.clear()
            scheduler.set_auto_refresh("codex", True, 120)
            await asyncio.wait_for(provider.read_started.wait(), 0.5)
            assert provider.read_calls == 3
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


def test_refresh_events_are_per_tool_and_busy_start_retries(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        settings = settings_factory(STOKE_AUTO_DEFAULT="1")
        codex, claude = FakeReader(), FakeReader()
        claude.name, claude.label = "claude", "Claude"
        codex.release = claude.release = asyncio.Event()
        providers = {"codex": codex, "claude": claude}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        waits: asyncio.Queue[float] = asyncio.Queue()
        original = asyncio.wait_for
        async def wait_for(awaitable: Any, timeout: float) -> Any:
            if asyncio.current_task().get_name() == "refresh:codex":
                waits.put_nowait(timeout)
            return await original(awaitable, timeout)
        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", wait_for)
        scheduler.set_auto_refresh("codex", True, 300)
        await scheduler.start()
        try:
            assert await original(waits.get(), 0.5) == 10
            assert codex.read_calls == claude.read_calls == 0
            codex.release.set()
            await asyncio.sleep(0)
            scheduler.set_auto_refresh("codex", True, 90)
            assert await original(waits.get(), 0.5) == 90
            assert codex.read_calls == 1 and claude.read_calls == 0
            scheduler.set_auto_refresh("claude", True, 60)
            await original(claude.read_started.wait(), 0.5)
            assert claude.read_calls == 1 and codex.read_calls == 1
        finally:
            await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_manual_reset_expiry_equal_to_now_is_rejected(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    current = int(datetime(2026, 10, 17, 23, 59, tzinfo=ZoneInfo("Europe/Istanbul")).timestamp())
    monkeypatch.setattr("stoke.clock.now", lambda: current)
    scheduler = scheduler_for(settings_factory(), FakeProvider())
    with pytest.raises(ValueError, match="expiry must be in the future"):
        scheduler.add_manual_reset("claude", "Full reset", "2026-10-17")


def test_manual_reset_add_sort_expiry_limit_remove_prune(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    current = [EPOCH]
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    scheduler = scheduler_for(settings_factory(), FakeProvider())
    state = scheduler.add_manual_reset("claude", "Full reset", "2026-10-20")
    scheduler.add_manual_reset("claude", "5-hour reset", "2026-10-17")
    early = state.manual_resets[0]
    assert early.title == "5-hour reset" and re.fullmatch(r"[0-9a-f]{12}", early.id)
    assert datetime.fromtimestamp(early.expires_at, scheduler.settings.timezone).isoformat() == "2026-10-17T23:59:00+03:00"
    assert early.added_at == EPOCH
    for _ in range(8):
        scheduler.add_manual_reset("claude", "Full reset", "2026-10-21")
    with pytest.raises(ValueError, match="10"):
        scheduler.add_manual_reset("claude", "Full reset", "2026-10-22")
    current[0] = early.expires_at
    scheduler.add_manual_reset("claude", "Full reset", "2026-10-22")
    assert len(state.manual_resets) == 10 and early not in state.manual_resets
    removed = state.manual_resets[-1]
    scheduler.remove_manual_reset("claude", removed.id)
    assert len(state.manual_resets) == 9 and removed not in state.manual_resets
    with pytest.raises(ValueError, match="not found"):
        scheduler.remove_manual_reset("claude", "unknown")
    current[0] = state.manual_resets[-1].expires_at
    scheduler.remove_manual_reset("claude", state.manual_resets[-1].id)
    assert state.manual_resets == []
    assert load(scheduler.path, scheduler.settings, scheduler.providers) == scheduler.states


@pytest.mark.parametrize("title,date", [("bad", "2026-10-20"), ([], "2026-10-20"), ("Full reset", "bad"), ("Full reset", "2026-02-30"), ("Full reset", "20261020"), ("Full reset", None), ("Full reset", "2026-10-16")])
def test_manual_reset_invalid(title: Any, date: Any, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: EPOCH)
    scheduler = scheduler_for(settings_factory(), FakeProvider())
    with pytest.raises(ValueError):
        scheduler.add_manual_reset("claude", title, date)
    assert scheduler.states["claude"].manual_resets == []


def test_manual_reset_unsupported(settings_factory: Callable[..., Settings]) -> None:
    scheduler = scheduler_for(settings_factory())
    for operation in (lambda: scheduler.add_manual_reset("codex", "Full reset", "2026-10-20"), lambda: scheduler.remove_manual_reset("codex", "a")):
        with pytest.raises(ManualResetsUnsupported):
            operation()


@pytest.mark.parametrize("delta,warning", [(30 * 86400, None), (30 * 86400 - 1, "token_expiring"), (0, "token_expiring"), (-1, "token_expiring")])
def test_live_warning_priority_expiry_boundary_and_auth(delta: int, warning: str | None, settings_factory: Callable[..., Settings]) -> None:
    settings = replace(settings_factory(), claude_token_expires=1000 + delta)
    scheduler = scheduler_for(settings, FakeProvider())
    state = scheduler.states["claude"]
    def live() -> dict[str, Any]:
        return serialize_tool(state, 1000, settings=settings, manual_resets_allowed=True, auth_labels=ClaudeProvider.auth_labels)
    assert live()["warning"] == warning and state.warning is None
    assert live()["auth"] == {"stoke": {"label": "1-year token", "expires_at": 1000 + delta}, "read": {"label": "Browser login"}}
    assert live()["manual_resets_allowed"]
    state.last_read = ReadRecord(900, False, "usage reader signed out", True)
    assert live()["warning"] == "read_signed_out" and state.warning is None
    for persisted in ("auto_stopped", "did_not_open_window", "limits_unreadable", "stoke_failing"):
        state.warning = persisted
        assert live()["warning"] == persisted
    state.manual_resets = [ManualReset("0123456789ab", "Full reset", 1000, 900), ManualReset("abcdef012345", "5-hour reset", 1001, 900)]
    assert len(live()["manual_resets"]) == 1
    persisted = serialize_tool(state)
    assert len(persisted["manual_resets"]) == 2 and "auth" not in persisted and "manual_resets_allowed" not in persisted


def test_dry_run_preserves_both_capabilities_and_auth(settings_factory: Callable[..., Settings]) -> None:
    providers = create_providers(settings_factory())
    for name, real in (("claude", ClaudeProvider), ("codex", CodexProvider)):
        assert callable(providers[name].read_limits)
        assert providers[name].manual_resets is real.manual_resets
        assert providers[name].auth_labels == real.auth_labels
    async def no_wait(seconds: float) -> None:
        pass
    providers["claude"].sleep = no_wait
    assert asyncio.run(providers["claude"].read_limits()).resets is None


def test_api_new_endpoints_and_live_fields(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: EPOCH)
    async def scenario() -> None:
        settings = settings_factory(STOKE_CLAUDE_TOKEN_EXPIRES="2026-10-20")
        providers = create_providers(settings)
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        app = create_app(scheduler, settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            tools = (await client.get("/api/state")).json()["tools"]
            assert tools["claude"]["refreshable"] and tools["claude"]["manual_resets_allowed"]
            assert tools["claude"]["warning"] == "token_expiring"
            assert tools["claude"]["auth"]["stoke"]["expires_at"] == settings.claude_token_expires
            assert tools["codex"]["auth"] == {"shared": {"label": "ChatGPT login (device code)"}}
            assert tools["codex"]["warning"] is None and not tools["codex"]["manual_resets_allowed"]
            for action, body in (("auto-refresh", {"enabled": True, "seconds": 90}), ("resets", {"title": "Full reset", "expires_on": "2026-10-20"}), ("resets/remove", {"id": "unknown"})):
                assert (await client.post(f"/api/tools/claude/{action}", json=body)).status_code == 403
                assert (await client.post(f"/api/tools/unknown/{action}", headers=HEADERS, json=body)).status_code == 404
            result = await client.post("/api/tools/claude/auto-refresh", headers=HEADERS, json={"enabled": True, "seconds": 86400})
            assert result.status_code == 200 and result.json()["auto_refresh"] == {"enabled": True, "seconds": 86400}
            assert load(scheduler.path, settings, providers)["claude"].auto_refresh == AutoRefresh(True, 86400)
            result = await client.post("/api/tools/claude/resets", headers=HEADERS, json={"title": "Full reset", "expires_on": "2026-10-20"})
            assert result.status_code == 200
            id = result.json()["manual_resets"][0]["id"]
            result = await client.post("/api/tools/claude/resets/remove", headers=HEADERS, json={"id": id})
            assert result.status_code == 200 and result.json()["manual_resets"] == []
            assert (await client.post("/api/tools/claude/resets/remove", headers=HEADERS, json={"id": id})).status_code == 422
            for action in ("resets", "resets/remove"):
                assert (await client.post(f"/api/tools/codex/{action}", headers=HEADERS, json={})).status_code == 404
    asyncio.run(scenario())


@pytest.mark.parametrize("action,body", [
    ("auto-refresh", {}), ("auto-refresh", {"enabled": True, "seconds": 90, "extra": 1}),
    ("auto-refresh", {"enabled": 1, "seconds": 90}), ("auto-refresh", {"enabled": True, "seconds": True}),
    ("auto-refresh", {"enabled": True, "seconds": 90.0}), ("auto-refresh", {"enabled": True, "seconds": 59}),
    ("auto-refresh", {"enabled": False, "seconds": 86401}),
    ("resets", {}), ("resets", {"title": "bad", "expires_on": "2026-10-20"}),
    ("resets", {"title": "Full reset", "expires_on": "bad"}), ("resets", {"title": [], "expires_on": "2026-10-20"}),
    ("resets", {"title": "Full reset", "expires_on": "2026-10-20", "extra": 1}),
    ("resets/remove", {}), ("resets/remove", {"id": 1}), ("resets/remove", {"id": "unknown"}),
    ("resets/remove", {"id": "unknown", "extra": True}),
])
def test_api_new_endpoint_invalid_bodies(action: str, body: Any, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        scheduler = scheduler_for(settings_factory(), FakeReader())
        # Give the fake both capabilities to exercise strict HTTP validation.
        scheduler.providers["codex"].manual_resets = True
        app = create_app(scheduler, scheduler.settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for value in (body, [], None, True):
                result = await client.post(f"/api/tools/codex/{action}", headers=HEADERS, json=value)
                assert result.status_code == 422 and result.json()["detail"]
            assert (await client.post(f"/api/tools/codex/{action}", headers=HEADERS, content="invalid")).status_code == 422
    asyncio.run(scenario())


def test_api_auto_refresh_unsupported(settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        scheduler = scheduler_for(settings_factory(), FakeProvider())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, scheduler.settings)), base_url="http://test") as client:
            assert (await client.post("/api/tools/claude/auto-refresh", headers=HEADERS, json={"enabled": True, "seconds": 300})).status_code == 404
    asyncio.run(scenario())
