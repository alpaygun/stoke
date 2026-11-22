"""Independent Antigravity keep-open scheduling and state contracts."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from stoke.api import create_app
from stoke.config import Settings
from stoke.models import (
    GroupStokeResult, LimitsSnapshot, PendingVerify, Plan, QuotaGroup, ScopedLimit, StokeRecord,
    StokeResult, ThirdPartyState, Window, serialize_tool,
)
from stoke.providers import create_providers
from stoke.providers.base import ProviderError, ReaderSignedOut, StokeNotDelivered, StokeUnverified
from stoke.scheduler import STALE_RESET_LIMIT, AlreadyRunning, Scheduler
from stoke.store import load, save

NAME = "antigravity"
NOW = 1_800_000_000
WINDOW = 18_000
WEEK = 604_800
DELAY = 30
HEADERS = {"X-Stoke": "1"}


def observation(now: int = NOW, group: QuotaGroup | None = None) -> LimitsSnapshot:
    idle = QuotaGroup("third_party", None, None, True, True)
    return LimitsSnapshot(
        Window(12, now + WINDOW, 300), Window(19, now + WEEK, 10080), None, observed_at=now,
        scoped=[ScopedLimit("Argon", 9, now + WEEK)], groups=[group or idle], window_confirmed=True,
    )


def confirmed(now: int = NOW, *, end: int | None = None) -> GroupStokeResult:
    window = Window(2, now + WINDOW if end is None else end, 300)
    group = QuotaGroup("third_party", window, Window(1, now + WEEK, 10080))
    return GroupStokeResult(observation(now, group), window, 17)


class FakeAntigravity:
    name = NAME
    label = "Antigravity"
    reset_closes_window = False
    auth_labels = {"shared": {"label": "Account login"}}
    third_party_group = True
    serial_cli = True

    def __init__(self) -> None:
        self.main: StokeResult | Exception = StokeResult(Window(1, NOW + WINDOW, 300), None, 12)
        self.third: GroupStokeResult | Exception = confirmed()
        self.usage: LimitsSnapshot = observation()
        self.calls: list[str] = []
        self.started = asyncio.Event()
        self.block: str | None = None
        self.release = asyncio.Event()
        self.busy = False

    async def _call[T](self, operation: str, result: T | Exception) -> T:
        assert not self.busy, "Concurrent CLI operations"
        self.busy = True
        self.calls.append(operation)
        try:
            if self.block == operation:
                self.started.set()
                await self.release.wait()
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.busy = False

    async def stoke(self) -> StokeResult:
        return await self._call("gemini", self.main)

    async def stoke_third_party(self) -> GroupStokeResult:
        return await self._call("prompt", self.third)

    async def verify_third_party(self, delivered: bool) -> GroupStokeResult:
        return await self._call(f"verify:{delivered}", self.third)

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        return await self._call("read", self.usage)


@dataclass
class FakeClock:
    now: int = NOW


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr("stoke.clock.now", lambda: clock.now)
    return clock


@pytest.fixture
def setup(settings_factory: Callable[..., Settings], clock: FakeClock) -> tuple[Scheduler, FakeAntigravity]:
    settings = settings_factory(STOKE_TOOLS=NAME, STOKE_TOOL_DELAYS=f"{NAME}={DELAY}")
    vendor = FakeAntigravity()
    path = settings.data_dir / "state.json"
    providers = {NAME: vendor}
    return Scheduler(providers, load(path, settings, providers), settings, path), vendor


def restart(scheduler: Scheduler) -> Scheduler:
    states = load(scheduler.path, scheduler.settings, scheduler.providers)
    return Scheduler(scheduler.providers, states, scheduler.settings, scheduler.path)


def activate(scheduler: Scheduler) -> ThirdPartyState:
    scheduler.set_auto(NAME, True)
    tp = scheduler.set_third_party(NAME, "keep_open").third_party
    assert tp is not None
    return tp


@pytest.mark.parametrize("mode", ["hide", "show"])
def test_inactive_restart_never_sends_or_verifies_persisted_prompt(setup, mode) -> None:
    scheduler, vendor = setup
    state = scheduler.states[NAME]
    state.auto_stoke = True
    state.five_hour = Window(8, NOW + WINDOW, 300)
    state.third_party = ThirdPartyState(
        mode, next_stoke_at=NOW, pending_verify=PendingVerify(NOW - 30, True), warning="signed_out",
    )
    save(scheduler.path, scheduler.states)
    scheduler = restart(scheduler)

    async def scenario() -> None:
        await scheduler.start()
        try:
            await asyncio.sleep(0)
            await scheduler._attempt_third_party(NAME)
            tp = scheduler.states[NAME].third_party
            assert tp.next_stoke_at is tp.warning is None
            assert tp.pending_verify == PendingVerify(NOW - 30, True)
            assert vendor.calls == []
        finally:
            await scheduler.stop()

    asyncio.run(scenario())


def test_loop_stokes_reschedules_and_records_its_own_cycle(setup) -> None:
    scheduler, vendor = setup
    state = scheduler.states[NAME]
    state.five_hour = Window(8, NOW + WINDOW - 500, 300)
    tp = activate(scheduler)
    tp.five_hour = Window(10, NOW - DELAY, 300)

    async def scenario() -> None:
        await scheduler.start()
        try:
            await asyncio.sleep(0)
            assert vendor.calls == ["prompt"]
            assert state.next_stoke_at == NOW + WINDOW - 500 + DELAY
            assert tp.next_stoke_at == NOW + WINDOW + DELAY
            assert state.last_stoke is state.last_read is tp.warning is tp.pending_verify is None
            assert tp.last_stoke == StokeRecord(NOW, "auto", True, None, True, 17)
            cycle, = scheduler.cycles.read(NOW, tool=f"{NAME}-3p", days=1)
            assert (cycle["tool"], cycle["previous_end"], cycle["new_end"], cycle["delay_seconds"]) == (
                f"{NAME}-3p", NOW - DELAY, NOW + WINDOW, DELAY,
            )
        finally:
            await scheduler.stop()

    asyncio.run(scenario())


def test_3p_logs_success_and_failed_verification(setup, clock, caplog: pytest.LogCaptureFixture) -> None:
    scheduler, vendor = setup
    scheduler.states[NAME].five_hour = Window(8, NOW + WINDOW - 500, 300)
    tp = activate(scheduler)

    with caplog.at_level("INFO", logger="stoke.scheduler"):
        asyncio.run(scheduler._attempt_third_party(NAME))
        resets_at = datetime.fromtimestamp(tp.five_hour.resets_at, timezone.utc).isoformat()
        clock.now = tp.next_stoke_at
        tp.pending_verify = PendingVerify(clock.now - 1, True)
        vendor.third = ReaderSignedOut("Antigravity signed out")
        asyncio.run(scheduler._attempt_third_party(NAME))

    prefix = f"tool={NAME}-3p trigger=auto"
    assert [(record.levelname, record.getMessage()) for record in caplog.records
            if record.getMessage().startswith(prefix)] == [
        ("INFO", f"{prefix} ok=True duration_ms=17 resets_at={resets_at}"),
        ("INFO", f"{prefix} ok=False duration_ms={tp.last_stoke.duration_ms} verify=1 error=signed out"),
    ]


def test_shared_read_adopts_both_windows_and_clears_an_old_3p_warning(setup) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    tp.last_stoke = StokeRecord(NOW - 60, "auto", False, "signed out", None, 5)
    tp.warning = "signed_out"
    tp.pending_verify = PendingVerify(NOW - 60, True)
    tp.consecutive_failures = 4
    result = confirmed()
    vendor.usage = result.snapshot
    state = asyncio.run(scheduler.refresh_limits(NAME))
    assert state.five_hour == result.snapshot.five_hour
    assert state.weekly == result.snapshot.weekly and state.scoped_limits == result.snapshot.scoped
    assert tp.five_hour == result.window
    assert state.next_stoke_at == tp.next_stoke_at == NOW + WINDOW + DELAY
    assert tp.warning is tp.warning_reason is tp.pending_verify is None
    assert tp.consecutive_failures == 0
    assert scheduler.events[NAME].is_set() and scheduler.third_party_events[NAME].is_set()

    async def scenario() -> None:
        resumed = restart(scheduler)
        await resumed.start()
        try:
            await asyncio.sleep(0)
            assert resumed.states[NAME].third_party.warning is None
            assert vendor.calls == ["read"]
        finally:
            await resumed.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "unverified", "not-delivered"])
def test_gemini_stoke_shares_third_party_observations(setup, outcome) -> None:
    scheduler, vendor = setup
    snapshot = confirmed().snapshot
    vendor.main = {
        "success": StokeResult(snapshot.five_hour, snapshot.weekly, 5, groups=snapshot.groups, snapshot=snapshot),
        "unverified": StokeUnverified("unverified", delivered=True, snapshot=snapshot),
        "not-delivered": StokeNotDelivered("not delivered", snapshot=snapshot),
    }[outcome]
    state = asyncio.run(scheduler.stoke_now(NAME, "manual"))
    assert state.quota_groups == snapshot.groups
    assert state.third_party.five_hour == snapshot.groups[0].five_hour
    assert state.last_read is None and state.third_party.next_stoke_at is None
    assert state.last_stoke.ok == (outcome == "success")
    assert vendor.calls == ["gemini"]


def test_3p_result_updates_gemini_with_read_rules_without_touching_last_read(setup) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    state = scheduler.states[NAME]
    original_hour = Window(60, NOW + 100, 300)
    original_week = Window(80, NOW + WEEK, 10080)
    state.five_hour, state.weekly, state.limits_at = original_hour, original_week, NOW - 1
    state.next_stoke_at = original_hour.resets_at + DELAY
    asyncio.run(scheduler._attempt_third_party(NAME))
    assert state.five_hour == Window(12, original_hour.resets_at, 300)
    assert state.next_stoke_at == original_hour.resets_at + DELAY
    assert state.weekly == vendor.third.snapshot.weekly
    assert (state.weekly_resets[0].from_at, state.weekly_resets[0].to_at) == (NOW - 1, NOW)
    assert state.last_read is None and state.scoped_limits == vendor.third.snapshot.scoped
    assert state.limits_at == NOW and tp.last_stoke.ok


@pytest.mark.parametrize("missing", [True, False])
def test_verification_backoff_shares_failed_reads_and_exhausts_after_three(setup, clock, missing) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    snapshot = observation()
    if missing:
        snapshot.groups = [QuotaGroup("third_party", None, Window(1, NOW + WEEK, 10080))]
    vendor.third = StokeUnverified("unverified", delivered=True, snapshot=snapshot)
    for failure in range(4):
        started = clock.now
        asyncio.run(scheduler._attempt_third_party(NAME))
        assert tp.consecutive_failures == failure + 1
        assert tp.last_stoke.at == NOW and not tp.last_stoke.ok
        assert tp.next_stoke_at == started + scheduler.settings.retry_seconds[failure]
        assert tp.pending_verify == (PendingVerify(NOW, True, failure) if failure < 3 else None)
        clock.now = tp.next_stoke_at
    assert vendor.calls == ["prompt", "verify:True", "verify:True", "verify:True"]
    assert tp.warning == tp.warning_reason == ("limits_unreadable" if missing else "did_not_open_window")
    assert scheduler.states[NAME].quota_groups == snapshot.groups
    assert scheduler.states[NAME].last_read is None


def test_stale_3p_success_backs_off_without_disabling_auto(setup) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    vendor.third = confirmed(end=NOW)
    asyncio.run(scheduler._attempt_third_party(NAME))
    assert tp.last_stoke.error == "reset time not in the future"
    assert tp.five_hour is None and tp.next_stoke_at == NOW + scheduler.settings.retry_seconds[0]
    assert scheduler.states[NAME].auto_stoke


def test_sign_out_during_verification_raises_own_warning_and_respects_read_limit(setup, clock) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    tp.pending_verify = PendingVerify(NOW - 10, True)
    vendor.third = ReaderSignedOut("Antigravity signed out")
    for reads in range(1, 4):
        asyncio.run(scheduler._attempt_third_party(NAME))
        assert tp.warning == "signed_out" and tp.last_stoke.error == "signed out"
        assert tp.pending_verify == (PendingVerify(NOW - 10, True, reads) if reads < 3 else None)
        clock.now = tp.next_stoke_at
    assert vendor.calls == ["verify:True"] * 3
    assert scheduler.states[NAME].warning is None


def test_crash_marker_is_persisted_before_prompt_and_restart_only_verifies(setup) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)

    async def scenario() -> None:
        vendor.block = "prompt"
        running = asyncio.create_task(scheduler._attempt_third_party(NAME))
        await vendor.started.wait()
        persisted = json.loads(scheduler.path.read_text(encoding="utf-8"))[NAME]["third_party"]
        assert persisted["pending_verify"] == {"attempt_at": NOW, "delivered": True, "reads": 0}
        assert "running" not in persisted and tp.running
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        resumed = restart(scheduler)
        await resumed._attempt_third_party(NAME)
        assert vendor.calls == ["prompt", "verify:True"]
        assert resumed.states[NAME].third_party.pending_verify is None

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["mode", "auto", "adopt"])
def test_queued_3p_rechecks_after_the_usage_read(setup, change) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    vendor.block = "read"
    if change == "adopt":
        vendor.usage = confirmed().snapshot

    async def scenario() -> None:
        reading = asyncio.create_task(scheduler.refresh_limits(NAME))
        await vendor.started.wait()
        queued = asyncio.create_task(scheduler._attempt_third_party(NAME))
        await asyncio.sleep(0)
        assert not tp.running and not queued.done()
        if change == "mode":
            scheduler.set_third_party(NAME, "show")
        elif change == "auto":
            scheduler.set_auto(NAME, False)
        vendor.release.set()
        await asyncio.gather(reading, queued)

    asyncio.run(scenario())
    assert vendor.calls == ["read"]
    assert tp.next_stoke_at == (NOW + WINDOW + DELAY if change == "adopt" else None)


def test_activation_adopts_legacy_quota_and_refreshes_percent_without_changing_end(setup) -> None:
    scheduler, vendor = setup
    state = scheduler.states[NAME]
    result = confirmed()
    state.quota_groups, state.limits_at = result.snapshot.groups, NOW - 100
    tp = activate(scheduler)
    assert tp.five_hour == result.window and tp.next_stoke_at == NOW + WINDOW + DELAY
    vendor.usage = observation(group=QuotaGroup("third_party", Window(9, NOW + WINDOW + 100, 300), None))
    asyncio.run(scheduler.refresh_limits(NAME))
    assert tp.five_hour == Window(9, NOW + WINDOW, 300)
    vendor.usage = observation()
    asyncio.run(scheduler.refresh_limits(NAME))
    assert tp.five_hour == Window(9, NOW + WINDOW, 300)


@pytest.mark.parametrize("trigger", ["auto", "startup", "reset", "planned", "manual"])
def test_main_waits_for_3p_and_rechecks_eligibility_and_attempt_time(setup, clock, trigger) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    state = scheduler.states[NAME]
    vendor.block = "prompt"
    if trigger == "planned":
        state.plan = Plan("07:00", "once", None, NOW, None)

    async def scenario() -> None:
        third = asyncio.create_task(scheduler._attempt_third_party(NAME))
        await vendor.started.wait()
        with pytest.raises(AlreadyRunning):
            await scheduler.refresh_limits(NAME)
        main = asyncio.create_task(scheduler.stoke_now(NAME, trigger))
        await asyncio.sleep(0)
        assert tp.running and not state.running and not main.done()
        if trigger == "planned":
            scheduler.cancel_plan(NAME)
        elif trigger != "manual":
            scheduler.set_auto(NAME, False)
        clock.now += 7
        vendor.release.set()
        await asyncio.gather(third, main)

    asyncio.run(scenario())
    if trigger == "manual":
        assert vendor.calls == ["prompt", "gemini"]
        assert state.last_stoke.at == NOW + 7
    else:
        assert vendor.calls == ["prompt"] and state.last_stoke is None


def test_queued_main_auto_skips_when_3p_snapshot_adopts_gemini_window(setup) -> None:
    scheduler, vendor = setup
    activate(scheduler)
    vendor.block = "prompt"

    async def scenario() -> None:
        third = asyncio.create_task(scheduler._attempt_third_party(NAME))
        await vendor.started.wait()
        main = asyncio.create_task(scheduler.stoke_now(NAME, "startup"))
        await asyncio.sleep(0)
        vendor.release.set()
        await asyncio.gather(third, main)

    asyncio.run(scenario())
    assert vendor.calls == ["prompt"]
    assert scheduler.states[NAME].next_stoke_at == NOW + WINDOW + DELAY


@pytest.mark.parametrize("enabled", [True, False])
def test_plan_auto_after_reschedules_3p_without_sending_it(setup, enabled) -> None:
    scheduler, vendor = setup
    state = scheduler.set_auto(NAME, not enabled)
    tp = scheduler.set_third_party(NAME, "keep_open").third_party
    if not enabled:
        tp.pending_verify = PendingVerify(NOW - 10, True)
        tp.warning = "stoke_failing"
    state.plan = Plan("07:00", "once", enabled, NOW, None)
    for event in (scheduler.events[NAME], scheduler.third_party_events[NAME]):
        event.clear()
    asyncio.run(scheduler.stoke_now(NAME, "planned"))
    assert vendor.calls == ["gemini"]
    assert state.auto_stoke is enabled
    assert tp.next_stoke_at == (NOW if enabled else None)
    assert tp.warning is None
    if not enabled:
        assert tp.pending_verify == PendingVerify(NOW - 10, True)
    assert scheduler.events[NAME].is_set() and scheduler.third_party_events[NAME].is_set()


def test_stale_auto_stop_pauses_3p_and_success_can_resume_it(setup) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    state = scheduler.states[NAME]
    state.stale_resets = STALE_RESET_LIMIT - 1
    vendor.main = StokeResult(Window(1, NOW, 300), None, 10)
    asyncio.run(scheduler.stoke_now(NAME, "auto"))
    assert tp.next_stoke_at is None and not state.auto_stoke
    vendor.main = StokeResult(Window(1, NOW + WINDOW, 300), None, 10)
    asyncio.run(scheduler.stoke_now(NAME, "manual"))
    assert state.auto_stoke and tp.next_stoke_at == NOW
    assert vendor.calls == ["gemini", "gemini"]


def test_success_does_not_recover_auto_after_user_turns_it_off_mid_attempt(setup) -> None:
    scheduler, vendor = setup
    state = scheduler.set_third_party(NAME, "keep_open")
    state.stale_resets = STALE_RESET_LIMIT
    vendor.block = "gemini"

    async def scenario() -> None:
        main = asyncio.create_task(scheduler.stoke_now(NAME, "manual"))
        await vendor.started.wait()
        scheduler.set_auto(NAME, False)
        vendor.release.set()
        await main

    asyncio.run(scenario())
    assert not state.auto_stoke and state.third_party.next_stoke_at is None


@pytest.mark.parametrize("failure", [False, True])
def test_inactive_completion_preserves_record_but_leaves_no_schedule_or_warning(setup, failure) -> None:
    scheduler, vendor = setup
    tp = activate(scheduler)
    vendor.block = "prompt"
    if failure:
        vendor.third = StokeUnverified("unverified", delivered=True, snapshot=observation())

    async def scenario() -> None:
        third = asyncio.create_task(scheduler._attempt_third_party(NAME))
        await vendor.started.wait()
        scheduler.set_third_party(NAME, "hide")
        vendor.release.set()
        await third

    asyncio.run(scenario())
    assert tp.last_stoke.ok is not failure
    assert tp.five_hour == (None if failure else vendor.third.window)
    assert tp.next_stoke_at is tp.warning is None and not tp.consecutive_failures
    assert tp.pending_verify == (PendingVerify(NOW, True) if failure else None)
    scheduler.set_third_party(NAME, "keep_open")
    if failure:
        vendor.third = confirmed()
        asyncio.run(scheduler._attempt_third_party(NAME))
        assert vendor.calls == ["prompt", "verify:True"]


def test_store_round_trip_and_live_fields(setup) -> None:
    scheduler, _ = setup
    tp = activate(scheduler)
    asyncio.run(scheduler._attempt_third_party(NAME))
    tp.running = True
    save(scheduler.path, scheduler.states)
    restored = restart(scheduler).states[NAME]
    assert not restored.third_party.running
    assert serialize_tool(restored) == serialize_tool(scheduler.states[NAME])
    live = serialize_tool(restored, NOW, settings=scheduler.settings)
    assert live["capabilities"]["third_party"]
    assert live["third_party"].keys() - serialize_tool(restored)["third_party"].keys() == {
        "running", "window_open", "active",
    }
    assert live["third_party"]["window_open"] and live["third_party"]["active"]
    assert not serialize_tool(restored, NOW + WINDOW, settings=scheduler.settings)["third_party"]["window_open"]


def test_dry_run_shares_both_windows_and_expires_them_independently(settings_factory, clock) -> None:
    seconds = 300
    settings = settings_factory(STOKE_TOOLS=NAME, STOKE_DRY_RUN_WINDOW_SECONDS=str(seconds))
    provider = create_providers(settings)[NAME]

    async def no_wait(seconds: float) -> None:
        pass

    provider.sleep = no_wait
    assert provider.serial_cli and provider.third_party_group
    main = asyncio.run(provider.stoke())
    assert main.five_hour.resets_at == NOW + seconds
    assert main.groups == [QuotaGroup("third_party", None, None, True, True)]
    clock.now += 100
    group = asyncio.run(provider.stoke_third_party())
    assert group.window.resets_at == clock.now + seconds
    assert group.snapshot.five_hour == main.five_hour
    assert asyncio.run(provider.verify_third_party(True)).window == group.window
    clock.now = main.five_hour.resets_at
    read = asyncio.run(provider.read_limits())
    assert read.groups[0].five_hour == group.window and not read.window_confirmed
    clock.now = group.window.resets_at
    read = asyncio.run(provider.read_limits())
    assert read.groups[0].five_hour is None and read.groups[0].five_hour_synthetic
    assert not read.groups[0].weekly_synthetic


def test_dry_run_failure_setting_fails_both_stokes(settings_factory) -> None:
    settings = settings_factory(STOKE_TOOLS=NAME, STOKE_DRY_RUN_FAIL=NAME)
    provider = create_providers(settings)[NAME]

    async def no_wait(seconds: float) -> None:
        pass

    provider.sleep = no_wait
    for operation in (provider.stoke, provider.stoke_third_party):
        with pytest.raises(ProviderError, match="^dry-run failure$"):
            asyncio.run(operation())


@pytest.mark.parametrize("value", [None, [], 42, {"five_hour": "invalid"},
                                  {"last_stoke": {}}, {"pending_verify": {"reads": 5}},
                                  {"mode": ["keep_open"], "warning": {}}, {"mode": "unknown"}])
def test_malformed_store_keeps_main_state_and_defaults_3p(setup, value, caplog) -> None:
    scheduler, _ = setup
    state = scheduler.states[NAME]
    state.five_hour = Window(8, NOW + 5, 300)
    save(scheduler.path, scheduler.states)
    data = serialize_tool(state)
    if value is not None:
        data["third_party"] = value
    else:
        del data["third_party"]
    scheduler.path.write_text(json.dumps({NAME: data}), encoding="utf-8")
    restored = restart(scheduler).states[NAME]
    assert restored.five_hour == state.five_hour and restored.third_party == ThirdPartyState()
    assert "using defaults" not in caplog.text


def test_api_mode_validation_capability_cycle_filter_and_no_rate_limit(settings_factory, clock) -> None:
    settings = settings_factory(STOKE_TOOLS=f"{NAME},claude")
    providers = create_providers(settings)
    path = settings.data_dir / "state.json"
    scheduler = Scheduler(providers, load(path, settings, providers), settings)
    url = f"/api/tools/{NAME}/third-party"
    with TestClient(create_app(scheduler, settings)) as client:
        assert client.get("/api/state").json()["tools"][NAME]["third_party"]["mode"] == "show"
        for invalid in ([], {}, {"mode": []}, {"mode": "bad"}, {"mode": True}, {"mode": "show", "extra": 0}):
            assert client.post(url, headers=HEADERS, json=invalid).status_code == 422
        assert client.post(url, headers=HEADERS, content="{").status_code == 422
        assert client.post(url, json={"mode": "show"}).status_code == 403
        for mode in ("hide", "show", "keep_open") * 12:
            response = client.post(url, headers=HEADERS, json={"mode": mode})
            assert response.status_code == 200
            tp = response.json()["third_party"]
            assert tp["mode"] == mode and not tp["active"]
        assert client.post("/api/tools/claude/third-party", headers=HEADERS, json={"mode": "show"}).status_code == 404
        assert client.post(f"/api/tools/{NAME}-3p/auto", headers=HEADERS, json={"enabled": True}).status_code == 404
        assert client.get(f"/api/cycles?tool={NAME}-3p").json() == {"cycles": []}
        assert client.get("/api/cycles?tool=unknown").status_code == 404
    assert restart(scheduler).states[NAME].third_party.mode == "keep_open"
