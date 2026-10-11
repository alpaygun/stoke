"""Pure scheduler rules and lifecycle/concurrency behavior without real waits."""

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Mapping

import pytest

from conftest import FakeProvider
from stoke.config import Settings
from stoke.models import StokeRecord, StokeResult, ToolState, Window
from stoke.providers.base import LimitsUnreadable, ProviderError
from stoke.scheduler import AlreadyRunning, Scheduler, compute_warning, next_after_failure, next_after_success, opened_new_window, startup_decision
from stoke.store import load, save


def make_scheduler(settings: Settings, provider: FakeProvider) -> Scheduler:
    """Create a scheduler with isolated default state."""
    providers = {provider.name: provider}
    return Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)


def test_next_after_success(settings_factory: Callable[..., Settings]) -> None:
    assert next_after_success(StokeResult(Window(1, 2000, 300), None, 3), settings_factory()) == 2090


@pytest.mark.parametrize("failures,delay", [(1, 60), (2, 300), (3, 900), (4, 1800), (5, 3600), (6, 3600), (1000, 3600)])
def test_backoff_growth_and_cap(failures: int, delay: int, settings_factory: Callable[..., Settings]) -> None:
    assert next_after_failure(1000, failures, settings_factory()) == 1000 + delay


@pytest.mark.parametrize("reset,expected", [(None, "stoke_now"), (999, "stoke_now"), (1000, "stoke_now"), (1001, 1091)])
def test_startup_decision(reset: int | None, expected: str | int, settings_factory: Callable[..., Settings]) -> None:
    state = ToolState("claude", "Claude", True, False, None if reset is None else Window(1, reset, 300), None, None, None, 0, None)
    assert startup_decision(state, 1000, settings_factory()) == expected


@pytest.mark.parametrize("previous_reset,attempt_at,new_reset,expected", [
    (None, 1000, 19000, None), (None, 1000, 4600, None),
    (1000, 999, 18999, None),
    (1000, 1000, 19000, True),
    (1000, 1090, 19090, True), (1000, 1090, 4690, False),
    (1000, 1900, 19900, True), (1000, 1900, 16300, True),
    (1000, 1900, 16299, False), (1000, 1901, 19901, None),
    (1000, 8200, 26200, None), (1000, 8200, 11800, None),
])
def test_all_rule_seven_cases(
    previous_reset: int | None, attempt_at: int, new_reset: int, expected: bool | None,
) -> None:
    previous = None if previous_reset is None else Window(1, previous_reset, 300)
    assert opened_new_window(previous, attempt_at, Window(1, new_reset, 300)) is expected


@pytest.mark.parametrize("opened,error,failures,expected", [
    (None, None, 0, None), (True, None, 0, None), (None, "failed", 2, None),
    (None, "failed", 3, "stoke_failing"), (None, "limits unreadable", 1, "limits_unreadable"),
    (None, "limits unreadable", 3, "limits_unreadable"),
    (False, None, 0, "did_not_open_window"),
    (False, "limits unreadable", 3, "did_not_open_window"),
])
def test_warning_priority(opened: bool | None, error: str | None, failures: int, expected: str | None) -> None:
    state = ToolState("claude", "Claude", True, False, None, None,
                      StokeRecord(1000, "auto", error is None, error, opened, 2), None, failures, None)
    assert compute_warning(state) == expected
    state.last_stoke = None
    assert compute_warning(state) == ("stoke_failing" if failures >= 3 else None)


def test_startup_with_stored_future_window_schedules(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = settings_factory(STOKE_AUTO_DEFAULT="1")
        provider = FakeProvider()
        scheduler = make_scheduler(settings, provider)
        scheduler.states["claude"].five_hour = Window(10, 2000, 300)
        save(scheduler.path, scheduler.states)
        scheduler = Scheduler({"claude": provider}, load(scheduler.path, settings, {"claude": provider}), settings)
        await scheduler.start()
        await asyncio.sleep(0)
        assert provider.calls == 0
        assert scheduler.states["claude"].next_stoke_at == 2090
        assert len(scheduler.tasks) == 1
        await scheduler.stop()
        assert not scheduler.tasks
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


@pytest.mark.parametrize("reset", [None, 999, 1000])
def test_startup_without_open_window_stokes_immediately(
    reset: int | None, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), FakeProvider())
        provider = scheduler.providers["claude"]
        if reset is not None:
            scheduler.states["claude"].five_hour = Window(10, reset, 300)
        save(scheduler.path, scheduler.states)
        scheduler.states = load(scheduler.path, scheduler.settings, scheduler.providers)
        await scheduler.start()
        await provider.started.wait()
        state = scheduler.states["claude"]
        assert provider.calls == 1 and state.last_stoke.trigger == "startup"
        assert state.last_stoke.opened_new_window is (None if reset is None else True)
        assert state.next_stoke_at == 19090 and not state.running
        await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_enable_wakes_idle_loop_and_uses_startup_trigger(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        scheduler = make_scheduler(settings_factory(), provider)
        await scheduler.start()
        await asyncio.sleep(0)
        assert not provider.calls
        scheduler.set_auto("claude", True)
        await provider.started.wait()
        assert scheduler.states["claude"].last_stoke.trigger == "startup"
        scheduler.set_auto("claude", False)
        await asyncio.sleep(0)
        assert scheduler.states["claude"].next_stoke_at is None
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"].auto_stoke is False
        await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_manual_completion_wakes_and_replaces_pending_timer(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = settings_factory(STOKE_AUTO_DEFAULT="1")
        provider = FakeProvider(result=StokeResult(Window(1, 3000, 300), None, 2))
        scheduler = make_scheduler(settings, provider)
        scheduler.states["claude"].five_hour = Window(1, 2000, 300)
        observed: list[float] = []
        waiting = asyncio.Event()
        original_wait_for = asyncio.wait_for
        async def observe_wait(awaitable: object, timeout: float) -> object:
            observed.append(timeout)
            waiting.set()
            return await original_wait_for(awaitable, timeout)
        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", observe_wait)
        await scheduler.start()
        await waiting.wait()
        waiting.clear()
        assert observed == [1090]
        await scheduler.stoke_now("claude", "manual")
        await waiting.wait()
        assert observed[-1] == 2090
        assert scheduler.states["claude"].last_stoke.opened_new_window is None
        scheduler.set_auto("claude", False)
        await asyncio.sleep(0)
        assert scheduler.states["claude"].next_stoke_at is None
        await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_success_sets_limits_at_to_completion_time(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [1000]
    async def scenario() -> None:
        result = StokeResult(Window(2, 19000, 300), Window(14, 40000, 10080), 12)
        provider = FakeProvider(result=result)
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["claude"]
        state.limits_at = 900
        attempt = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
        await provider.started.wait()
        assert state.limits_at == 900
        current[0] = 1012
        provider.release.set()
        await attempt
        assert state.last_stoke.ok and state.last_stoke.at == 1000
        assert state.limits_at == 1012
        assert (state.five_hour, state.weekly) == (result.five_hour, result.weekly)
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"].limits_at == 1012
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    asyncio.run(scenario())


@pytest.mark.parametrize("error,warning", [
    (ProviderError("failed"), "stoke_failing"),
    (LimitsUnreadable("different text"), "limits_unreadable"),
    (ValueError("unexpected"), "stoke_failing"),
])
def test_failures_keep_windows_and_use_completion_time_for_backoff(
    error: Exception, warning: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [1000]
    async def scenario() -> None:
        provider = FakeProvider(result=error)
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        state = scheduler.states["claude"]
        state.five_hour, state.weekly = Window(3, 999, 300), Window(4, 40000, 10080)
        state.limits_at = 900
        previous = (state.five_hour, state.weekly)
        for count, expected_delay in [(1, 60), (2, 300), (3, 900)]:
            provider.started.clear()
            provider.release.clear()
            attempt = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
            await provider.started.wait()
            assert state.running
            current[0] += 2
            provider.release.set()
            await attempt
            assert not state.running and state.last_stoke.ok is False
            assert (state.five_hour, state.weekly) == previous
            assert state.limits_at == 900
            assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"].limits_at == 900
            assert state.consecutive_failures == count
            assert state.next_stoke_at == current[0] + expected_delay
        assert state.warning == warning
        if isinstance(error, LimitsUnreadable):
            assert state.last_stoke.error == "limits unreadable"
        provider.result = StokeResult(Window(1, 20000, 300), None, 2)
        await scheduler.stoke_now("claude", "manual")
        assert state.consecutive_failures == 0 and state.warning is None
        assert state.weekly is None
        assert state.limits_at == current[0]
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    asyncio.run(scenario())


def test_per_tool_lock_and_independent_tools(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        claude, codex = FakeProvider(), FakeProvider("codex")
        claude.release = asyncio.Event()
        settings = settings_factory()
        providers = {"claude": claude, "codex": codex}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        attempt = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
        await claude.started.wait()
        with pytest.raises(AlreadyRunning):
            await scheduler.stoke_now("claude", "manual")
        state = await scheduler.stoke_now("codex", "manual")
        assert state.last_stoke.ok and claude.calls == 1 and codex.calls == 1
        assert scheduler.states["claude"].running
        scheduler.set_auto("claude", False)
        claude.release.set()
        await attempt
        assert scheduler.states["claude"].next_stoke_at is None
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_shutdown_cancels_running_attempts_and_clears_running(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        await scheduler.start()
        await provider.started.wait()
        tasks = list(scheduler.tasks.values())
        await scheduler.stop()
        assert all(task.cancelled() for task in tasks)
        assert not scheduler.states["claude"].running
        assert not scheduler.locks["claude"].locked()
        assert not load(scheduler.path, scheduler.settings, scheduler.providers)["claude"].running
        # Manual tasks are also owned for shutdown even when no loops are active.
        provider.started.clear()
        manual = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
        await provider.started.wait()
        await scheduler.stop()
        assert manual.cancelled() and not scheduler.states["claude"].running
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_enabling_auto_during_manual_attempt_does_not_relabel_future_attempt(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [1000]
    async def scenario() -> None:
        provider = FakeProvider()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        await scheduler.start()
        manual = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
        await provider.started.wait()
        scheduler.set_auto("claude", True)
        provider.release.set()
        await manual
        assert scheduler.states["claude"].next_stoke_at == 19090
        provider.started.clear()
        provider.result = StokeResult(Window(1, 40000, 300), None, 2)
        current[0] = 19090
        scheduler.events["claude"].set()
        await provider.started.wait()
        assert scheduler.states["claude"].last_stoke.trigger == "auto"
        assert provider.calls == 2
        await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    asyncio.run(scenario())


def test_each_auto_loop_progresses_independently(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = settings_factory(STOKE_AUTO_DEFAULT="1")
        claude, codex = FakeProvider(), FakeProvider("codex")
        codex.release = asyncio.Event()
        providers = {"claude": claude, "codex": codex}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        await scheduler.start()
        await claude.started.wait()
        await codex.started.wait()
        assert len(scheduler.tasks) == 2
        assert scheduler.states["claude"].last_stoke.ok
        assert not scheduler.states["claude"].running
        assert scheduler.states["codex"].running
        await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_log_records_are_single_lines_and_redact_errors(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        scheduler = make_scheduler(settings_factory(), provider)
        await scheduler.stoke_now("claude", "manual")
        provider.result = ProviderError("failure\nwith sk-ant-fixture-secret and eyJ" + "a" * 30)
        await scheduler.stoke_now("claude", "manual")
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    caplog.set_level("INFO", logger="stoke.scheduler")
    asyncio.run(scenario())
    logs = [record.getMessage() for record in caplog.records if record.name == "stoke.scheduler"]
    assert len(logs) == 2 and all("\n" not in line for line in logs)
    assert "tool=claude trigger=manual ok=True duration_ms=12 resets_at=1970-01-01T05:16:40+00:00" == logs[0]
    assert "ok=False" in logs[1] and "[REDACTED]" in logs[1]
    assert "sk-ant-" not in logs[1] and "eyJ" not in logs[1]


@pytest.mark.parametrize("recovery", ["timeout", "event", "cancel"])
def test_loop_survives_persistence_failure_and_wait_is_interruptible(
    recovery: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        await scheduler.start()
        loop = scheduler.tasks["claude"]
        original_save = save
        saves = 0

        def fail_once(path: Path, states: Mapping[str, ToolState]) -> None:
            nonlocal saves
            saves += 1
            if saves == 1:
                raise OSError("disk full: private path sk-ant-fixture-secret")
            original_save(path, states)

        monkeypatch.setattr("stoke.scheduler.store.save", fail_once)
        original_wait_for = asyncio.wait_for
        retry_waiting = asyncio.Event()
        timeouts: list[float] = []

        async def observe_wait(awaitable: Awaitable[bool], timeout: float) -> bool:
            timeouts.append(timeout)
            if len(timeouts) == 1:
                retry_waiting.set()
                if recovery == "timeout":
                    return await original_wait_for(awaitable, 0)
            return await original_wait_for(awaitable, timeout)

        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", observe_wait)
        try:
            await original_wait_for(retry_waiting.wait(), 0.5)
            assert timeouts[0] == scheduler.settings.retry_seconds[0]
            assert not loop.done() and not scheduler.states["claude"].running
            assert not scheduler.locks["claude"].locked()
            if recovery == "cancel":
                await scheduler.stop()
                assert loop.cancelled() and provider.calls == 0
            else:
                if recovery == "event":
                    scheduler.events["claude"].set()
                await original_wait_for(provider.started.wait(), 0.5)
                assert provider.calls == 1 and not loop.done()
                assert scheduler.states["claude"].last_stoke.ok
                assert scheduler.states["claude"].next_stoke_at == 19090
        finally:
            await scheduler.stop()

    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    caplog.set_level("ERROR", logger="stoke.scheduler")
    asyncio.run(scenario())
    errors = [record for record in caplog.records if record.name == "stoke.scheduler" and record.levelname == "ERROR"]
    assert [record.getMessage() for record in errors] == ["tool=claude loop error: OSError"]
    assert all(record.exc_info is None and record.stack_info is None for record in errors)
    assert "private path" not in caplog.text and "sk-ant-" not in caplog.text
