"""Pure scheduler rules and lifecycle/concurrency behavior without real waits."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeProvider, FakeReader
from stoke.config import Settings
from stoke.models import ReadRecord, LimitsSnapshot, Plan, PlanOutcome, ResetCredit, ResetCredits, StokeRecord, StokeResult, ToolState, Window
from stoke.providers.base import LimitsUnreadable, ProviderError
from stoke.scheduler import REFRESH_BUSY_RETRY_SECONDS, STALE_RESET_LIMIT, AlreadyRunning, RefreshUnsupported, Scheduler, compute_warning, next_after_failure, next_after_success, opened_new_window, startup_decision
from stoke.store import load, save
from stoke.scheduler import PLAN_GRACE_SECONDS, PLAN_QUIET_SECONDS, PlanConflict, PlanUnreachable, effective_next, first_target, following_target, in_quiet_zone, plan_deadline, plan_due
from stoke.scheduler import detect_window_reset, known_window, reset_next


@pytest.mark.parametrize("previous,current,observed,expected", [
    (Window(10, 2000, 300), 5, 1000, True),
    (Window(10, 2000, 300), 6, 1000, False),
    (Window(10, 2000, 300), 5, 2000, False),
    (Window(10, 2000, 300), 5, 2001, False),
    (None, 0, 1000, False),
])
def test_early_reset_detection_boundaries(previous: Window | None, current: float, observed: int, expected: bool) -> None:
    assert detect_window_reset(previous, current, observed) is expected


@pytest.mark.parametrize("end,minutes,expected", [(2799, 30, 2889), (2800, 30, 2890), (2801, 30, 1000), (1001, 0, 1000), (19000, 300, 19090)])
def test_early_reset_next_threshold(end: int, minutes: int, expected: int, settings_factory: Callable[..., Settings]) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    state = scheduler.states["claude"]
    state.five_hour, state.reset_hold_minutes = Window(1, end, 300), minutes
    assert reset_next(state, 1000, scheduler.settings) == expected


@pytest.mark.parametrize("end,expected", [(2800, 2890), (2801, "stoke_now"), (1000, "stoke_now")])
def test_early_reset_startup_survives_restart(
    end: int, expected: str | int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        provider = FakeReader()
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        state = scheduler.states["codex"]
        state.five_hour, state.window_closed_at = Window(1, end, 300), 999
        save(scheduler.path, scheduler.states)
        scheduler.states = load(scheduler.path, scheduler.settings, scheduler.providers)
        state = scheduler.states["codex"]
        assert startup_decision(state, 1000, scheduler.settings) == expected
        await scheduler.start()
        assert state.next_stoke_at == (1000 if expected == "stoke_now" else expected)
        if expected == "stoke_now":
            await asyncio.wait_for(provider.started.wait(), 1)
            assert state.last_stoke.trigger == "startup"
        else:
            await asyncio.sleep(0)
            assert provider.calls == 0
        await scheduler.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("new_end,opened", [(15400, True), (15399, False), (4600, False)])
def test_early_reset_success_clears_marker_and_assesses_window(
    new_end: int, opened: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    provider = FakeProvider(result=StokeResult(Window(1, new_end, 300), None, 12))
    scheduler = make_scheduler(settings_factory(), provider)
    state = scheduler.states["claude"]
    state.five_hour, state.window_closed_at = Window(40, 19000, 300), 1000
    asyncio.run(scheduler.stoke_now("claude", "reset"))
    assert state.window_closed_at is None
    assert state.five_hour == provider.result.five_hour
    assert state.last_stoke.opened_new_window is opened
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state


@pytest.mark.parametrize("new_end,expected,unchanged", [
    (5880, None, True), (6000, None, True), (6120, None, True),
    (5400, None, True), (6600, None, True),
    (5399, False, False), (6601, False, False),
    (15400, True, False), (19000, True, False),
])
def test_early_reset_unchanged_window_end_skips_assessment(
    new_end: int, expected: bool | None, unchanged: bool,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    caplog.set_level("INFO", logger="stoke.scheduler")
    provider = FakeProvider(result=StokeResult(Window(1, new_end, 300), None, 12))
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
    state = scheduler.states["claude"]
    state.five_hour, state.window_closed_at = Window(40, 6000, 300), 1000
    asyncio.run(scheduler.stoke_now("claude", "reset"))
    assert state.last_stoke.ok and state.last_stoke.opened_new_window is expected
    assert state.warning == ("did_not_open_window" if expected is False else None)
    messages = [record.getMessage() for record in caplog.records if "window was not reset" in record.getMessage()]
    assert messages == (["tool=claude window was not reset"] if unchanged else [])
    assert all(record.levelname == "INFO" for record in caplog.records if "window was not reset" in record.getMessage())
    assert state.window_closed_at is None and state.five_hour == provider.result.five_hour
    assert state.next_stoke_at == new_end + scheduler.settings.delay_seconds
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state


@pytest.mark.parametrize("attempt_at", [1000, 1090])
def test_early_reset_hold_skips_new_window_assessment(
    attempt_at: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: attempt_at)
    provider = FakeProvider(result=StokeResult(Window(1, attempt_at + 3 * 3600, 300), None, 12))
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
    state = scheduler.states["claude"]
    state.five_hour, state.window_closed_at, state.next_stoke_at = Window(40, 1000, 300), 900, 1090
    asyncio.run(scheduler.stoke_now("claude", "auto"))
    assert state.last_stoke.ok and state.last_stoke.opened_new_window is None
    assert state.warning is None


@pytest.mark.parametrize("result", [ProviderError("failed"), LimitsUnreadable("unreadable"), StokeResult(Window(1, 1000, 300), None, 1)])
def test_early_reset_failure_keeps_marker(
    result: StokeResult | Exception, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    scheduler = make_scheduler(settings_factory(), FakeProvider(result=result))
    state = scheduler.states["claude"]
    state.five_hour, state.window_closed_at = Window(40, 19000, 300), 999
    asyncio.run(scheduler.stoke_now("claude", "reset"))
    assert not state.last_stoke.ok and state.window_closed_at == 999
    assert state.five_hour == Window(40, 19000, 300)
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state


def test_early_reset_plan_quiet_zone_and_known_window(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), FakeProvider())
    state = scheduler.states["claude"]
    state.five_hour, state.next_stoke_at = Window(40, 20000, 300), 1000
    assert known_window(state) is state.five_hour
    state.window_closed_at = 1000
    assert known_window(state) is None
    scheduler.set_plan("claude", "07:00", "once", None)
    assert state.plan.target_at == 18000
    assert effective_next(state, scheduler.settings) == (18000, True)
    assert not scheduler._expire_plan(state, 18900)
    assert scheduler._expire_plan(state, 18901)
    assert state.plan_outcome.result == "missed"


def test_early_reset_plan_retry_deadline_ignores_old_end(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    scheduler = make_scheduler(settings_factory(), FakeProvider(result=ProviderError("failed")))
    state = scheduler.states["claude"]
    state.five_hour, state.window_closed_at = Window(40, 19000, 300), 999
    state.plan = Plan("03:20", "once", None, 100, None)
    asyncio.run(scheduler.stoke_now("claude", "planned"))
    assert state.plan is None and state.plan_outcome.result == "missed"


@pytest.mark.parametrize("closed,auto,running,recompute", [(True, True, False, True), (False, True, False, False), (True, False, False, False), (True, True, True, False)])
def test_early_reset_setting_recomputes_only_when_idle_and_auto(
    closed: bool, auto: bool, running: bool, recompute: bool,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    scheduler = make_scheduler(settings_factory(), FakeReader())
    state = scheduler.states["codex"]
    state.five_hour, state.window_closed_at = Window(40, 2800, 300), 999 if closed else None
    state.auto_stoke, state.running = auto, running
    state.next_stoke_at = 2890 if auto else None
    before = state.next_stoke_at
    scheduler.set_reset_hold("codex", 0)
    assert state.next_stoke_at == (1000 if recompute else before)
    assert scheduler.events["codex"].is_set() is recompute
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"].reset_hold_minutes == 0
    if recompute:
        scheduler.events["codex"].clear()
        scheduler.set_reset_hold("codex", 30)
        assert state.next_stoke_at == 2890 and scheduler.events["codex"].is_set()


@pytest.mark.parametrize("end,next_at", [(None, 1060), (999, 1300), (1000, 1090), (1000, 1300)])
def test_early_reset_setting_preserves_schedule_after_old_end(
    end: int | None, next_at: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), FakeReader())
    state = scheduler.states["codex"]
    state.five_hour = None if end is None else Window(40, end, 300)
    state.window_closed_at, state.next_stoke_at = 900, next_at
    scheduler.set_reset_hold("codex", 0)
    assert state.next_stoke_at == next_at
    assert not scheduler.events["codex"].is_set()
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"] == state


@pytest.mark.parametrize("minutes", [True, False, -1, 301, "30", 30.0, None])
def test_early_reset_setting_strict_validation(minutes: object, settings_factory: Callable[..., Settings]) -> None:
    scheduler = make_scheduler(settings_factory(), FakeReader())
    with pytest.raises(ValueError):
        scheduler.set_reset_hold("codex", minutes)
    assert scheduler.states["codex"].reset_hold_minutes == 30
    assert not scheduler.path.exists()


def test_early_reset_setting_requires_reader(settings_factory: Callable[..., Settings]) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    with pytest.raises(RefreshUnsupported):
        scheduler.set_reset_hold("claude", 30)


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


@pytest.mark.parametrize("auto,stale,expected", [
    (False, 3, "did_not_open_window"), (False, 4, "auto_stopped"),
    (False, 5, "auto_stopped"), (True, 4, "did_not_open_window"),
])
def test_auto_stopped_warning_has_highest_priority(auto: bool, stale: int, expected: str) -> None:
    state = ToolState("claude", "Claude", auto, False, None, None,
                      StokeRecord(1000, "auto", False, "limits unreadable", False, 2),
                      None, 4, None, stale_resets=stale)
    assert compute_warning(state) == expected


def test_stale_resets_keep_limits_back_off_and_stop_at_four(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    current = [1000]
    async def scenario() -> None:
        provider = FakeProvider()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        state = scheduler.states["claude"]
        state.five_hour, state.weekly = Window(3, 999, 300), Window(4, 40000, 10080)
        state.limits_at = 900
        previous = (state.five_hour, state.weekly, state.limits_at)
        assert STALE_RESET_LIMIT == 4
        for count, delay in [(1, 60), (2, 300), (3, 900), (4, None), (5, None)]:
            provider.result = StokeResult(Window(99, current[0] + 1, 300), None, 12)
            provider.started.clear()
            provider.release.clear()
            attempt_at = current[0]
            attempt = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
            await provider.started.wait()
            current[0] += 2
            provider.release.set()
            await attempt
            assert not state.running and not state.last_stoke.ok
            assert state.last_stoke.at == attempt_at
            assert state.last_stoke.error == "reset time not in the future"
            assert state.last_stoke.opened_new_window is None
            assert (state.five_hour, state.weekly, state.limits_at) == previous
            assert state.consecutive_failures == count and state.stale_resets == count
            assert state.next_stoke_at == (current[0] + delay if delay is not None else None)
            assert state.auto_stoke is (count < 4)
            assert state.warning == ("auto_stopped" if count >= 4 else "stoke_failing" if count == 3 else None)
            assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    caplog.set_level("ERROR", logger="stoke.scheduler")
    asyncio.run(scenario())
    errors = [record for record in caplog.records if record.name == "stoke.scheduler" and record.levelname == "ERROR"]
    assert [record.getMessage() for record in errors] == ["tool=claude auto-stoke stopped: repeated stale reset times"]
    assert all(record.exc_info is None and record.stack_info is None for record in errors)


@pytest.mark.parametrize("reset,ok", [(999, False), (1000, False), (1001, True)])
def test_stale_reset_boundary(
    reset: int, ok: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        result = StokeResult(Window(2, reset, 300), None, 12)
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), FakeProvider(result=result))
        state = await scheduler.stoke_now("claude", "auto")
        assert state.last_stoke.ok is ok
        assert state.stale_resets == (0 if ok else 1)
        assert state.consecutive_failures == (0 if ok else 1)
        assert state.last_stoke.error == (None if ok else "reset time not in the future")
        assert state.next_stoke_at == (reset + scheduler.settings.delay_seconds if ok else 1060)
        assert state.five_hour == (result.five_hour if ok else None)
        assert state.limits_at == (1000 if ok else None)
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome,error,warning", [
    (StokeResult(Window(2, 19000, 300), None, 12), None, None),
    (ProviderError("reset time not in the future"), "reset time not in the future", "stoke_failing"),
    (LimitsUnreadable("no limits"), "limits unreadable", "limits_unreadable"),
    (ValueError("private text"), "unexpected provider error", "stoke_failing"),
])
def test_other_outcomes_break_stale_reset_streak(
    outcome: StokeResult | Exception, error: str | None, warning: str | None,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(99, 999, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        for _ in range(2):
            await scheduler.stoke_now("claude", "auto")
        state = scheduler.states["claude"]
        assert state.stale_resets == 2
        provider.result = outcome
        for count in range(3, 7):
            await scheduler.stoke_now("claude", "auto")
            assert state.stale_resets == 0 and state.auto_stoke
            assert state.last_stoke.error == error and state.warning == warning
            assert state.last_stoke.ok is (error is None)
            assert state.consecutive_failures == (0 if error is None else count)
            assert state.next_stoke_at == (19090 if error is None else next_after_failure(1000, count, scheduler.settings))
            assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


@pytest.mark.parametrize("reset,expected", [(999, 1000), (2000, 2090)])
def test_enabling_auto_after_stop_resets_counter_and_schedules(
    reset: int, expected: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(99, 999, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        state = scheduler.states["claude"]
        state.five_hour = Window(3, reset, 300)
        for _ in range(4):
            await scheduler.stoke_now("claude", "auto")
        assert not state.auto_stoke and state.warning == "auto_stopped"
        scheduler.set_auto("claude", True)
        assert state.stale_resets == 0 and state.warning == "stoke_failing"
        assert state.warning == compute_warning(state)
        assert state.auto_stoke and state.next_stoke_at == expected
        assert state.consecutive_failures == 4
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


@pytest.mark.parametrize("previous_stale", [0, 2, 4])
def test_user_disabling_auto_prevents_stale_manual_attempts_from_resuming_it(
    previous_stale: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(99, 999, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        state = scheduler.states["claude"]
        state.five_hour, state.weekly = Window(3, 999, 300), Window(4, 40000, 10080)
        state.limits_at = 900
        previous = (state.five_hour, state.weekly, state.limits_at)
        for _ in range(previous_stale):
            await scheduler.stoke_now("claude", "auto")
        assert state.stale_resets == previous_stale
        if previous_stale == 4:
            assert not state.auto_stoke and state.warning == "auto_stopped"
        scheduler.set_auto("claude", False)
        assert state.stale_resets == 0 and not state.auto_stoke
        assert state.next_stoke_at is None
        assert state.warning == ("stoke_failing" if previous_stale == 4 else None)
        assert state.warning == compute_warning(state)
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
        for count in range(1, 5):
            await scheduler.stoke_now("claude", "manual")
            assert state.stale_resets == 0 and not state.auto_stoke
            assert state.next_stoke_at is None
            assert state.consecutive_failures == previous_stale + count
            assert not state.last_stoke.ok and state.last_stoke.error == "reset time not in the future"
            assert state.last_stoke.opened_new_window is None
            assert (state.five_hour, state.weekly, state.limits_at) == previous
            assert state.warning == ("stoke_failing" if previous_stale + count >= 3 else None)
            assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
        provider.result = StokeResult(Window(2, 19000, 300), None, 12)
        await scheduler.stoke_now("claude", "manual")
        assert state.last_stoke.ok and not state.auto_stoke and state.next_stoke_at is None
        assert state.stale_resets == 0 and state.consecutive_failures == 0 and state.warning is None
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


@pytest.mark.parametrize("trigger", ["manual", "auto", "startup"])
def test_success_after_auto_stop_resumes_but_user_disabled_tool_stays_off(
    trigger: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(99, 999, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        for _ in range(4):
            await scheduler.stoke_now("claude", "auto")
        state = scheduler.states["claude"]
        assert not state.auto_stoke and state.stale_resets == 4
        await scheduler.stoke_now("claude", "manual")
        assert not state.auto_stoke and state.stale_resets == 5
        assert state.warning == "auto_stopped" and state.next_stoke_at is None
        provider.result = StokeResult(Window(2, 19000, 300), None, 12)
        await scheduler.stoke_now("claude", trigger)
        assert state.auto_stoke and state.next_stoke_at == 19090
        assert state.stale_resets == 0 and state.consecutive_failures == 0 and state.warning is None
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
        scheduler.set_auto("claude", False)
        await scheduler.stoke_now("claude", "manual")
        assert not state.auto_stoke and state.next_stoke_at is None
        assert state.stale_resets == 0 and state.warning is None
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_stale_reset_loop_stops_after_four_and_stays_idle_after_restart(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [1000]
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(99, 999, 300), None, 12))
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        original_wait_for = asyncio.wait_for
        timers: asyncio.Queue[float] = asyncio.Queue()
        idle = asyncio.Event()
        event = scheduler.events["claude"]
        original_wait = event.wait

        async def observe_timer(awaitable: Awaitable[bool], timeout: float) -> bool:
            timers.put_nowait(timeout)
            return await original_wait_for(awaitable, timeout)

        async def observe_idle() -> bool:
            if not scheduler.states["claude"].auto_stoke:
                idle.set()
            return await original_wait()

        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", observe_timer)
        monkeypatch.setattr(event, "wait", observe_idle)
        await scheduler.start()
        try:
            for count, delay in [(1, 60), (2, 300), (3, 900), (4, None)]:
                await original_wait_for(provider.started.wait(), 0.5)
                assert provider.calls == count
                provider.started.clear()
                provider.release.set()
                if delay is not None:
                    assert await original_wait_for(timers.get(), 0.5) == delay
                    assert scheduler.states["claude"].next_stoke_at == current[0] + delay
                    provider.release.clear()
                    current[0] += delay
                    event.set()
                else:
                    await original_wait_for(idle.wait(), 0.5)
            state = scheduler.states["claude"]
            assert state.stale_resets == 4 and state.warning == "auto_stopped"
            assert state.next_stoke_at is None and not state.auto_stoke
            assert provider.calls == 4 and not scheduler.tasks["claude"].done()
            idle.clear()
            current[0] += 86400
            event.set()
            await original_wait_for(idle.wait(), 0.5)
            assert provider.calls == 4 and timers.empty()
        finally:
            await scheduler.stop()
        scheduler = Scheduler(scheduler.providers, load(scheduler.path, scheduler.settings, scheduler.providers), scheduler.settings)
        idle.clear()
        original_wait = scheduler.events["claude"].wait
        monkeypatch.setattr(scheduler.events["claude"], "wait", observe_idle)
        await scheduler.start()
        try:
            await original_wait_for(idle.wait(), 0.5)
            assert provider.calls == 4 and timers.empty()
            assert scheduler.states["claude"].warning == "auto_stopped"
            assert not scheduler.states["claude"].auto_stoke
            assert scheduler.states["claude"].next_stoke_at is None
        finally:
            await scheduler.stop()
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    asyncio.run(scenario())


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


@pytest.mark.parametrize("five_hour", [None, Window(3, 20000, 300)])
@pytest.mark.parametrize("optional_data", [True, False])
def test_refresh_updates_only_usage_and_read_data_at_completion(
    five_hour: Window | None, optional_data: bool, settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    current = [1000]
    async def scenario() -> None:
        provider = FakeReader()
        provider.read_release = asyncio.Event()
        snapshot = provider.read_result
        assert isinstance(snapshot, LimitsSnapshot)
        if not optional_data:
            snapshot = replace(snapshot, weekly=None, resets=None)
            provider.read_result = snapshot
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["codex"]
        state.five_hour = deepcopy(five_hour)
        state.weekly = Window(14, 555555, 10080)
        state.resets = ResetCredits(2, [ResetCredit("Old reset", None, 888888)], 900)
        state.last_stoke = StokeRecord(800, "manual", False, "old failure", False, 42)
        state.next_stoke_at = 20090
        state.consecutive_failures = 3
        state.warning = "stoke_failing"
        state.stale_resets = 2
        state.auto_stoke = True
        state.limits_at = 900
        state.plan = Plan("07:00", "daily", False, 30000, 30060)
        state.plan_outcome = PlanOutcome(500, "missed", 600)
        before = deepcopy(state)
        caplog.clear()
        task = asyncio.create_task(scheduler.refresh_limits("codex"))
        await provider.read_started.wait()
        assert state.refreshing and not scheduler.locks["codex"].locked()
        current[0] = 1005
        provider.read_release.set()
        assert await task is state
        expected = replace(
            before, last_read=ReadRecord(1005, True, None, False), five_hour=None if five_hour is None else replace(five_hour, used_percent=8),
            weekly=snapshot.weekly if optional_data else before.weekly, limits_at=1005,
            resets=ResetCredits(1, snapshot.resets.credits, 1005) if optional_data else before.resets,
        )
        assert state == expected and not state.refreshing
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"] == state
        assert provider.calls == 0
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    caplog.set_level("DEBUG", logger="stoke.scheduler")
    asyncio.run(scenario())
    assert not [record for record in caplog.records if record.name == "stoke.scheduler" and record.levelno > 10]


@pytest.mark.parametrize("failure", [ProviderError("safe sk-ant-fixture-secret"), LimitsUnreadable("limits unreadable"), ValueError("private sk-ant-fixture-secret")])
def test_refresh_failure_preserves_state_and_safe_warning(
    failure: Exception, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        provider = FakeReader()
        provider.read_result = failure
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["codex"]
        state.five_hour = Window(4, 20000, 300)
        state.resets = ResetCredits(1, [], 900)
        before = deepcopy(state)
        save(scheduler.path, scheduler.states)
        previous_file = scheduler.path.read_bytes()
        caplog.clear()
        with pytest.raises(ProviderError) as error:
            await scheduler.refresh_limits("codex")
        if isinstance(failure, ProviderError):
            assert error.value is failure
        else:
            assert str(error.value) == "unexpected provider error"
        assert state.last_read is not None and not state.last_read.ok
        assert state.last_read.error == str(error.value)
        before.last_read = state.last_read
        assert state == before and scheduler.path.read_bytes() != previous_file
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"] == state
        assert not state.refreshing and not scheduler._attempts
    asyncio.run(scenario())
    expected = str(failure) if isinstance(failure, ProviderError) else "ValueError"
    assert [record.getMessage() for record in caplog.records] == [f"tool=codex refresh failed: {expected}"]
    assert "private" not in caplog.text and "sk-ant-" not in caplog.text


def test_refresh_overlap_rejected_but_stoke_can_start_during_refresh(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        provider = FakeReader()
        provider.read_release = asyncio.Event()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        refresh = asyncio.create_task(scheduler.refresh_limits("codex"))
        await provider.read_started.wait()
        with pytest.raises(AlreadyRunning):
            await scheduler.refresh_limits("codex")
        stoke = asyncio.create_task(scheduler.stoke_now("codex", "manual"))
        await provider.started.wait()
        assert scheduler.states["codex"].running and scheduler.states["codex"].refreshing
        provider.release.set()
        await stoke
        after_stoke = deepcopy(scheduler.states["codex"])
        provider.read_release.set()
        await refresh
        state = scheduler.states["codex"]
        assert state.five_hour == replace(after_stoke.five_hour, used_percent=8)
        assert state.last_stoke == after_stoke.last_stoke and state.next_stoke_at == after_stoke.next_stoke_at
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_refresh_rejected_while_stoke_running(settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        provider = FakeReader()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        stoke = asyncio.create_task(scheduler.stoke_now("codex", "manual"))
        await provider.started.wait()
        with pytest.raises(AlreadyRunning):
            await scheduler.refresh_limits("codex")
        assert provider.read_calls == 0 and not scheduler.states["codex"].refreshing
        provider.release.set()
        await stoke
    asyncio.run(scenario())


def test_refresh_capability_requires_callable_and_claude_is_unsupported(settings_factory: Callable[..., Settings]) -> None:
    provider = FakeProvider()
    provider.read_limits = None  # type: ignore[attr-defined]
    scheduler = make_scheduler(settings_factory(), provider)
    assert not scheduler.can_refresh("claude") and not scheduler.can_refresh("unknown")
    with pytest.raises(RefreshUnsupported):
        asyncio.run(scheduler.refresh_limits("claude"))


@pytest.mark.parametrize("periodic", [False, True])
def test_stop_cancels_inflight_refresh_and_clears_live_flag(periodic: bool, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        provider = FakeReader()
        provider.read_release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        if periodic:
            scheduler.set_auto_refresh("codex", True, 300)
            await scheduler.start()
            task = scheduler.refresh_tasks["codex"]
        else:
            task = asyncio.create_task(scheduler.refresh_limits("codex"))
        await provider.read_started.wait()
        assert scheduler.states["codex"].refreshing
        await scheduler.stop()
        assert task.cancelled() and not scheduler.states["codex"].refreshing and not scheduler._attempts
    asyncio.run(scenario())


def local_at(value: str, zone: str = "Europe/Istanbul") -> int:
    """Resolve a fixture's local date and time without the host clock."""
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).timestamp())


@pytest.mark.parametrize("current,expected", [
    ("2026-10-13T06:59:59", "2026-10-13T07:00"),
    ("2026-10-13T07:00", "2026-10-14T07:00"),
    ("2026-10-13T07:00:01", "2026-10-14T07:00"),
])
def test_first_target_strictly_after_now(current: str, expected: str) -> None:
    assert first_target("07:00", local_at(current), ZoneInfo("Europe/Istanbul")) == local_at(expected)


@pytest.mark.parametrize("date,next_date,hours", [
    ("2026-03-07", "2026-03-08", 23), ("2026-10-31", "2026-11-01", 25),
    ("2026-12-31", "2027-01-01", 24),
])
def test_following_target_uses_calendar_days(date: str, next_date: str, hours: int) -> None:
    target = local_at(date + "T07:00", "America/New_York")
    following = following_target(target, "07:00", ZoneInfo("America/New_York"))
    assert following == local_at(next_date + "T07:00", "America/New_York")
    assert following - target == hours * 3600
    assert first_target("07:00", target, ZoneInfo("America/New_York")) == following


@pytest.mark.parametrize("auto,base,retry,reset,expected,planned", [
    (True, 1999, None, None, 1999, False),
    (True, 2000, None, None, 20000, True),
    (True, 2100, None, None, 20000, True),
    (True, 20060, 20060, None, 20060, True),
    (False, 1999, None, None, 20000, True),
    (False, None, 20100, None, 20100, True),
    (True, 21000, None, 21000, 21090, True),
    (True, 21000, 20200, 21000, 20200, True),
])
def test_effective_next_plan_rules(
    auto: bool, base: int | None, retry: int | None, reset: int | None, expected: int, planned: bool,
    settings_factory: Callable[..., Settings],
) -> None:
    state = make_scheduler(settings_factory(), FakeProvider()).states["claude"]
    state.auto_stoke, state.next_stoke_at = auto, base
    state.plan = Plan("07:00", "daily", None, 20000, retry)
    state.five_hour = None if reset is None else Window(1, reset, 300)
    assert effective_next(state, settings_factory()) == (expected, planned)
    assert state.next_stoke_at == base


@pytest.mark.parametrize("auto,expected", [(True, (1000, False)), (False, (None, False))])
def test_effective_next_without_plan(auto: bool, expected: tuple[int | None, bool], settings_factory: Callable[..., Settings]) -> None:
    state = make_scheduler(settings_factory(), FakeProvider()).states["claude"]
    state.auto_stoke, state.next_stoke_at = auto, 1000
    assert effective_next(state, settings_factory()) == expected


@pytest.mark.parametrize("now,expected", [(1999, False), (2000, True), (19999, True), (20000, False)])
def test_quiet_zone_boundaries(now: int, expected: bool) -> None:
    assert PLAN_QUIET_SECONDS == 18000
    assert in_quiet_zone(Plan("07:00", "once", None, 20000, None), now) is expected


def test_plan_deadline_ignores_retry() -> None:
    plan = Plan("07:00", "daily", None, 20000, 25000)
    assert PLAN_GRACE_SECONDS == 900
    assert plan_due(plan, None, 90) == 25000
    assert plan_deadline(plan, None, 90) == 20900
    assert plan_deadline(plan, Window(1, 21000, 300), 90) == 21990
    assert plan_deadline(plan, Window(1, 19000, 300), 90) == 20900


@pytest.mark.parametrize("repeat", ["once", "daily"])
@pytest.mark.parametrize("auto_after", [None, True, False])
@pytest.mark.parametrize("trigger", ["planned", "manual"])
@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("offset", [0, 60])
def test_planned_success_applies_auto_and_closes_occurrence(
    repeat: str, auto_after: bool | None, trigger: str, auto: bool, offset: int,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    target = local_at("2026-10-13T07:00")
    monkeypatch.setattr("stoke.clock.now", lambda: target + offset)
    caplog.set_level("INFO", logger="stoke.scheduler")
    provider = FakeProvider(result=StokeResult(Window(1, target + 18000, 300), None, 12))
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1" if auto else "0"), provider)
    state = scheduler.states["claude"]
    state.plan = Plan("07:00", repeat, auto_after, target, target)
    state.stale_resets = 2
    asyncio.run(scheduler.stoke_now("claude", trigger))
    assert state.plan_outcome == PlanOutcome(target, "done", target + offset)
    expected_auto = auto if auto_after is None else auto_after
    assert state.auto_stoke is expected_auto
    assert state.next_stoke_at == (target + 18090 if expected_auto else None)
    assert state.stale_resets == 0
    assert state.last_stoke.ok and state.last_stoke.trigger == trigger
    assert state.plan == (Plan("07:00", "daily", auto_after, local_at("2026-10-14T07:00"), None) if repeat == "daily" else None)
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    logs = [record.getMessage() for record in caplog.records if " plan " in record.getMessage()]
    assert logs == ["tool=claude plan done target=2026-10-13T04:00:00+00:00"]


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("repeat", ["once", "daily"])
def test_planned_failure_retries_then_misses(
    auto: bool, repeat: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = local_at("2026-10-13T07:00")
    current = [target]
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1" if auto else "0"), FakeProvider(result=ProviderError("failed")))
    state = scheduler.states["claude"]
    state.plan = Plan("07:00", repeat, True, target, None)
    for offset, retry in [(0, 60), (60, 360), (360, None)]:
        current[0] = target + offset
        asyncio.run(scheduler.stoke_now("claude", "planned"))
        assert not state.last_stoke.ok and state.auto_stoke is auto
        assert state.next_stoke_at == (next_after_failure(current[0], state.consecutive_failures, scheduler.settings) if auto else None)
        if retry is None:
            assert state.plan == (None if repeat == "once" else Plan("07:00", "daily", True, target + 86400, None))
            assert state.plan_outcome == PlanOutcome(target, "missed", current[0])
        else:
            assert state.plan.retry_at == target + retry and state.plan_outcome is None
            assert effective_next(state, scheduler.settings) == (target + retry, True)


def test_retry_exactly_at_deadline_is_allowed_then_expires(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = local_at("2026-10-13T07:00")
    current = [target]
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    scheduler = make_scheduler(settings_factory(STOKE_RETRY_SECONDS="900"), FakeProvider(result=ProviderError("failed")))
    state = scheduler.states["claude"]
    state.plan = Plan("07:00", "once", None, target, None)
    asyncio.run(scheduler.stoke_now("claude", "planned"))
    assert state.plan.retry_at == target + 900
    current[0] += 900
    assert not scheduler._expire_plan(state, current[0])
    current[0] += 1
    assert scheduler._expire_plan(state, current[0])
    assert state.plan_outcome == PlanOutcome(target, "missed", current[0])


@pytest.mark.parametrize("repeat", ["once", "daily"])
def test_restart_expires_all_overdue_occurrences(
    repeat: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, current = local_at("2026-10-10T07:00"), local_at("2026-10-13T07:15:01")
    monkeypatch.setattr("stoke.clock.now", lambda: current)
    async def scenario() -> None:
        scheduler = make_scheduler(settings_factory(), FakeProvider())
        state = scheduler.states["claude"]
        state.plan = Plan("07:00", repeat, True, target, None)
        save(scheduler.path, scheduler.states)
        scheduler.states = load(scheduler.path, scheduler.settings, scheduler.providers)
        state = scheduler.states["claude"]
        await scheduler.start()
        try:
            await asyncio.sleep(0)
            assert state.plan_outcome == PlanOutcome(target if repeat == "once" else local_at("2026-10-13T07:00"), "missed", current)
            assert state.plan == (None if repeat == "once" else Plan("07:00", "daily", True, local_at("2026-10-14T07:00"), None))
            assert not state.auto_stoke and scheduler.providers["claude"].calls == 0
            assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("repeat", ["once", "daily"])
def test_manual_quiet_conflict_and_explicit_break(
    repeat: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, current = local_at("2026-10-13T07:00"), local_at("2026-10-13T03:00")
    monkeypatch.setattr("stoke.clock.now", lambda: current)
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(1, current + 18000, 300), None, 12))
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["claude"]
        state.plan = Plan("07:00", repeat, True, target, None)
        before = deepcopy(state)
        with pytest.raises(PlanConflict) as error:
            await scheduler.stoke_now("claude", "manual")
        assert error.value.target_at == target
        assert state == before and provider.calls == 0
        assert not scheduler._attempts and not scheduler.locks["claude"].locked()
        await scheduler.stoke_now("claude", "manual", break_plan=True)
        assert state.plan_outcome == PlanOutcome(target, "broken", current)
        assert not state.auto_stoke and state.next_stoke_at is None
        assert state.plan == (None if repeat == "once" else Plan("07:00", "daily", True, local_at("2026-10-14T07:00"), None))
        assert provider.calls == 1 and state.last_stoke.trigger == "manual"
    asyncio.run(scenario())


def test_manual_before_quiet_leaves_plan_and_auto_success_is_held(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = local_at("2026-10-13T01:59")
    monkeypatch.setattr("stoke.clock.now", lambda: current)
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1", STOKE_DELAY_SECONDS="0"), FakeProvider(result=StokeResult(Window(1, current + 18000, 300), None, 12)))
    state = scheduler.set_plan("claude", "07:00", "daily", False)
    before = deepcopy(state.plan)
    asyncio.run(scheduler.stoke_now("claude", "manual", break_plan=True))
    assert state.plan == before and state.plan_outcome is None
    asyncio.run(scheduler.stoke_now("claude", "auto"))
    assert state.plan == before and state.next_stoke_at == current + 18000
    assert effective_next(state, scheduler.settings) == (local_at("2026-10-13T07:00"), True)


@pytest.mark.parametrize("auto", [True, False])
@pytest.mark.parametrize("enable", [False, True])
def test_startup_in_quiet_zone_and_auto_off_plan_wait_for_target(
    auto: bool, enable: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [local_at("2026-10-13T03:00")]
    target = local_at("2026-10-13T07:00")
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(1, target + 18000, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1" if auto else "0"), provider)
        state = scheduler.states["claude"]
        state.five_hour = Window(1, current[0] - 1, 300)
        scheduler.set_plan("claude", "07:00", "once", False)
        timers: asyncio.Queue[float] = asyncio.Queue()
        original_wait_for = asyncio.wait_for
        async def observe(awaitable: Awaitable[bool], timeout: float) -> bool:
            timers.put_nowait(timeout)
            return await original_wait_for(awaitable, timeout)
        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", observe)
        await scheduler.start()
        try:
            assert await original_wait_for(timers.get(), 0.5) == target - current[0]
            assert provider.calls == 0
            assert state.next_stoke_at == (current[0] if auto else None)
            if enable:
                scheduler.set_auto("claude", True)
                assert await original_wait_for(timers.get(), 0.5) == target - current[0]
                assert provider.calls == 0
            current[0] = target
            scheduler.events["claude"].set()
            await original_wait_for(provider.started.wait(), 0.5)
            assert provider.calls == 1 and state.last_stoke.trigger == "planned"
            assert state.plan is None and state.plan_outcome.result == "done"
            assert not state.auto_stoke and state.next_stoke_at is None
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("reset,rejected", [(None, False), (0, False), (1, True)])
def test_set_plan_accept_reject_replace_persist_and_cancel(
    reset: int | None, rejected: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, target = local_at("2026-10-13T03:00"), local_at("2026-10-13T07:00")
    monkeypatch.setattr("stoke.clock.now", lambda: current)
    scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), FakeProvider())
    state = scheduler.states["claude"]
    state.next_stoke_at = current
    state.five_hour = None if reset is None else Window(1, target + reset, 300)
    state.plan = Plan("08:00", "once", None, target + 3600, 123)
    state.plan_outcome = PlanOutcome(1000, "broken", 1001)
    save(scheduler.path, scheduler.states)
    before = deepcopy(state)
    persisted = scheduler.path.read_bytes()
    scheduler.events["claude"].clear()
    outcome = deepcopy(state.plan_outcome)
    if rejected:
        def forbid_persist() -> None:
            pytest.fail("Rejected plans must not persist state")
        monkeypatch.setattr(scheduler, "_persist", forbid_persist)
        with pytest.raises(PlanUnreachable, match="^window open until 07:00, so 07:00 cannot be reached$"):
            scheduler.set_plan("claude", "07:00", "daily", True)
        assert state == before
        assert scheduler.path.read_bytes() == persisted
        assert not scheduler.events["claude"].is_set()
        return
    result = scheduler.set_plan("claude", "07:00", "daily", True)
    assert result is state
    assert state.plan == Plan("07:00", "daily", True, target, None)
    assert state.plan_outcome == outcome and state.next_stoke_at == current
    assert scheduler.events["claude"].is_set()
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state
    scheduler.set_auto("claude", False)
    assert state.plan is not None
    scheduler.set_auto("claude", True)
    base = state.next_stoke_at
    scheduler.events["claude"].clear()
    assert scheduler.cancel_plan("claude") is state
    assert effective_next(state, scheduler.settings) == (base, False)
    assert state.plan_outcome == outcome and scheduler.events["claude"].is_set()
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == state


@pytest.mark.parametrize("time,repeat,auto_after", [
    ("7:00", "daily", None), ("24:00", "daily", None), ("07:60", "daily", None),
    ("07:00:00", "daily", None), ("07:00\n", "daily", None), (None, "daily", None),
    ("07:00", "weekly", None), ("07:00", [], None), ("07:00", "daily", 1),
    ("07:00", "daily", "true"),
])
def test_set_plan_rejects_bad_values(
    time: object, repeat: object, auto_after: object, settings_factory: Callable[..., Settings],
) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    with pytest.raises(ValueError):
        scheduler.set_plan("claude", time, repeat, auto_after)
    assert scheduler.states["claude"].plan is None


def test_daily_loop_keeps_early_auto_stoke_then_waits_for_plan(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [local_at("2026-10-13T01:59")]
    target = local_at("2026-10-13T07:00")
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(1, current[0] + 18000, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1", STOKE_DELAY_SECONDS="0"), provider)
        state = scheduler.set_plan("claude", "07:00", "daily", None)
        timers: asyncio.Queue[float] = asyncio.Queue()
        original_wait_for = asyncio.wait_for
        async def observe(awaitable: Awaitable[bool], timeout: float) -> bool:
            timers.put_nowait(timeout)
            return await original_wait_for(awaitable, timeout)
        monkeypatch.setattr("stoke.scheduler.asyncio.wait_for", observe)
        await scheduler.start()
        try:
            assert await original_wait_for(timers.get(), 0.5) == target - current[0]
            assert provider.calls == 1 and state.last_stoke.trigger == "startup"
            assert state.plan_outcome is None
            current[0] = target
            provider.result = StokeResult(Window(1, target + 18000, 300), None, 12)
            scheduler.events["claude"].set()
            assert await original_wait_for(timers.get(), 0.5) == 18000
            assert provider.calls == 2 and state.last_stoke.trigger == "planned"
            assert state.plan_outcome == PlanOutcome(target, "done", target)
            assert state.plan.target_at == target + 86400
            assert effective_next(state, scheduler.settings) == (target + 18000, False)
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


def test_cancel_plan_wakes_held_auto_loop(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = local_at("2026-10-13T03:00")
    monkeypatch.setattr("stoke.clock.now", lambda: current)
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(1, current + 18000, 300), None, 12))
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        scheduler.set_plan("claude", "07:00", "once", None)
        await scheduler.start()
        try:
            await asyncio.sleep(0)
            assert provider.calls == 0
            scheduler.cancel_plan("claude")
            await asyncio.wait_for(provider.started.wait(), 0.5)
            assert provider.calls == 1 and scheduler.states["claude"].last_stoke.trigger == "startup"
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["replace", "cancel", "none"])
def test_inflight_attempt_does_not_close_a_replacement_plan(
    change: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = local_at("2026-10-13T07:00")
    current = [target]
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    async def scenario() -> None:
        provider = FakeProvider(result=StokeResult(Window(1, target + 18000, 300), None, 12))
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["claude"]
        state.plan = Plan("07:00", "once", True, target, None)
        task = asyncio.create_task(scheduler.stoke_now("claude", "manual"))
        await provider.started.wait()
        current[0] += 901
        assert not scheduler._expire_plan(state, current[0])
        if change == "replace":
            scheduler.set_plan("claude", "08:00", "daily", False)
        elif change == "cancel":
            scheduler.cancel_plan("claude")
        pending = deepcopy(state.plan)
        provider.release.set()
        await task
        if change == "none":
            assert state.plan is None and state.plan_outcome == PlanOutcome(target, "done", current[0])
            assert state.auto_stoke
        else:
            assert state.plan == pending and state.plan_outcome is None and not state.auto_stoke
    asyncio.run(scenario())
