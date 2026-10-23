"""Permanent weekly reset history, credit classification and shared dismissal."""

import asyncio
import json
import re
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, Callable

import httpx
import pytest

from conftest import FakeProvider, FakeReader
from stoke.api import create_app
from stoke.config import Settings
from stoke.models import LimitsSnapshot, Plan, ResetCredit, ResetCredits, StokeResult, WeeklyReset, Window, serialize_tool
from stoke.providers.base import ProviderError
from stoke.scheduler import Scheduler, detect_weekly_reset
from stoke.store import load, save

HEADERS = {"X-Stoke": "1"}
OLD_WEEK = Window(70, 40000, 10080)
NEW_WEEK = Window(5, 126400, 10080)
CREDIT = ResetCredit("Full reset", 10, 700000)


def make_scheduler(settings: Settings, provider: FakeProvider) -> Scheduler:
    providers = {provider.name: provider}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    state = scheduler.states[provider.name]
    state.five_hour, state.weekly, state.limits_at = Window(42, 2000, 300), replace(OLD_WEEK), 900
    return scheduler


def event(**changes: Any) -> WeeklyReset:
    return replace(WeeklyReset("abcdef012345", "early", 900, 1000, 70, 40000, 5, 126400), **changes)


@pytest.mark.parametrize("previous,previous_at,current,observed,expected", [
    (OLD_WEEK, 900, Window(70, 126400, 10080), 1000, True),
    (OLD_WEEK, 900, Window(70, 126401, 10080), 1000, True),
    (OLD_WEEK, 900, Window(60, 40000, 10080), 1000, True),
    (OLD_WEEK, 900, Window(59, 40000, 10080), 1000, True),
    (OLD_WEEK, 900, Window(61, 126399, 10080), 1000, False),
    (OLD_WEEK, 900, Window(70, 40000, 10080), 1000, False),
    (OLD_WEEK, 900, NEW_WEEK, 40000, False),
    (OLD_WEEK, 900, NEW_WEEK, 40001, False),
    (None, 900, NEW_WEEK, 1000, False),
    (OLD_WEEK, None, NEW_WEEK, 1000, False),
    (OLD_WEEK, 900, None, 1000, False),
    (OLD_WEEK, 0, NEW_WEEK, 1000, True),
])
def test_weekly_detection_boundaries(
    previous: Window | None, previous_at: int | None, current: Window | None, observed: int, expected: bool,
) -> None:
    assert detect_weekly_reset(previous, previous_at, current, observed) is expected


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_weekly_stoke_records_once_and_preserves_history(
    name: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    caplog.set_level("INFO", logger="stoke.scheduler")
    provider = FakeProvider(name, StokeResult(Window(2, 19000, 300), NEW_WEEK, 12))
    scheduler = make_scheduler(settings_factory(), provider)
    state = scheduler.states[name]
    older = event(id="000000000001", to_at=800, dismissed=True)
    state.weekly_resets = [older]

    async def scenario() -> None:
        await scheduler.stoke_now(name, "manual")
        recorded = state.weekly_resets[0]
        assert re.fullmatch(r"[0-9a-f]{12}", recorded.id)
        assert recorded == event(id=recorded.id)
        assert state.weekly_resets[1] is older
        assert load(scheduler.path, scheduler.settings, scheduler.providers)[name] == state
        await scheduler.stoke_now(name, "manual")
        assert state.weekly_resets == [recorded, older]
        assert not scheduler._attempts
    asyncio.run(scenario())
    messages = [record.getMessage() for record in caplog.records if "weekly reset detected" in record.getMessage()]
    assert messages == [f"tool={name} weekly reset detected kind=early"]


@pytest.mark.parametrize("result", [
    ProviderError("failed"), RuntimeError("private"),
    StokeResult(Window(2, 1000, 300), NEW_WEEK, 12),
    StokeResult(Window(2, 19000, 300), None, 12),
])
def test_weekly_stoke_rejected_or_missing_values_never_detect(
    result: StokeResult | Exception, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    scheduler = make_scheduler(settings_factory(), FakeProvider(result=result))
    before = deepcopy(scheduler.states["claude"])
    state = asyncio.run(scheduler.stoke_now("claude", "manual"))
    assert state.weekly_resets == []
    if isinstance(result, Exception) or result.five_hour.resets_at <= 1000:
        assert (state.weekly, state.limits_at) == (before.weekly, before.limits_at)


@pytest.mark.parametrize("observed", [899, 900, 950, 2000])
@pytest.mark.parametrize("name", ["claude", "codex"])
def test_weekly_refresh_acceptance_and_scheduling(
    observed: int, name: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    provider = FakeReader()
    provider.name = name
    provider.read_result = LimitsSnapshot(Window(8, 99999, 300), NEW_WEEK, None, observed)
    scheduler = make_scheduler(settings_factory(), provider)
    state = scheduler.states[name]
    state.auto_stoke, state.next_stoke_at, state.consecutive_failures, state.stale_resets = True, 2090, 2, 1
    state.plan = Plan("07:00", "daily", True, 30000, 31000)
    before = deepcopy(state)
    asyncio.run(scheduler.refresh_limits(name))
    assert len(state.weekly_resets) == (0 if observed < 900 else 1)
    assert (state.five_hour.resets_at, state.next_stoke_at, state.consecutive_failures, state.stale_resets,
            state.plan, state.plan_outcome, state.auto_stoke, state.warning) == (
        before.five_hour.resets_at, before.next_stoke_at, before.consecutive_failures, before.stale_resets,
        before.plan, before.plan_outcome, before.auto_stoke, before.warning,
    )
    if observed >= 900:
        assert state.weekly_resets[0] == event(id=state.weekly_resets[0].id, to_at=min(observed, 1000))
        asyncio.run(scheduler.refresh_limits(name))
        assert len(state.weekly_resets) == 1
    else:
        assert (state.weekly, state.limits_at) == (before.weekly, before.limits_at)
    assert load(scheduler.path, scheduler.settings, scheduler.providers)[name] == state


@pytest.mark.parametrize("previous,current,expected", [
    ([CREDIT], [], "credit"),
    ([replace(CREDIT, expires_at=950)], [], "early"),
    ([replace(CREDIT, expires_at=1000)], [], "early"),
    ([CREDIT], [replace(CREDIT)], "early"),
    ([CREDIT], [replace(CREDIT, title="Other")], "credit"),
    ([CREDIT], [replace(CREDIT, granted_at=11)], "credit"),
    ([CREDIT], [replace(CREDIT, expires_at=700001)], "credit"),
    ([], [], "early"), (None, [], "early"),
])
def test_weekly_refresh_credit_classification(
    previous: list[ResetCredit] | None, current: list[ResetCredit], expected: str,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    caplog.set_level("INFO", logger="stoke.scheduler")
    provider = FakeReader()
    provider.read_result = LimitsSnapshot(Window(8, 99999, 300), NEW_WEEK, ResetCredits(len(current), current, 0))
    scheduler = make_scheduler(settings_factory(), provider)
    state = scheduler.states["codex"]
    state.resets = None if previous is None else ResetCredits(len(previous), previous, 900)
    asyncio.run(scheduler.refresh_limits("codex"))
    assert state.weekly_resets == [event(id=state.weekly_resets[0].id, kind=expected)]
    assert f"tool=codex weekly reset detected kind={expected}" in caplog.messages


@pytest.mark.parametrize("credits,expected", [([], "credit"), ([CREDIT], "early"), (None, "early")])
def test_weekly_stoke_pending_followup_decides_without_changing_schedule(
    credits: list[ResetCredit] | None, expected: str,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        provider = FakeReader()
        provider.result = StokeResult(Window(2, 19000, 300), NEW_WEEK, 12)
        provider.read_release = asyncio.Event()
        provider.read_result = LimitsSnapshot(
            Window(8, 99999, 300), NEW_WEEK,
            None if credits is None else ResetCredits(len(credits), credits, 0),
        )
        scheduler = make_scheduler(settings_factory(STOKE_AUTO_DEFAULT="1"), provider)
        state = scheduler.states["codex"]
        state.resets = ResetCredits(1, [replace(CREDIT)], 900)
        await scheduler.stoke_now("codex", "manual")
        pending = state.weekly_resets[0]
        assert pending == event(id=pending.id, credits_before=[CREDIT])
        assert pending.credits_before is not state.resets.credits
        assert pending.credits_before[0] is not state.resets.credits[0]
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"].weekly_resets == [pending]
        assert scheduler._attempts
        before = deepcopy(state)
        await provider.read_started.wait()
        assert not state.auto_refresh.enabled and state.refreshing and not state.running
        provider.read_release.set()
        await asyncio.gather(*scheduler._attempts)
        assert pending.kind == expected
        assert pending.credits_before == ([CREDIT] if credits is None else None)
        assert (state.five_hour.resets_at, state.next_stoke_at, state.plan, state.plan_outcome,
                state.consecutive_failures, state.stale_resets) == (
            before.five_hour.resets_at, before.next_stoke_at, before.plan, before.plan_outcome,
            before.consecutive_failures, before.stale_resets,
        )
        assert provider.calls == 1 and provider.read_calls == 1
        assert not scheduler._attempts
        assert load(scheduler.path, scheduler.settings, scheduler.providers)["codex"].weekly_resets == [pending]
    asyncio.run(scenario())


@pytest.mark.parametrize("observed,resets,kind,pending", [
    (899, True, "early", True), (1000, False, "early", True), (1000, True, "credit", False),
])
def test_weekly_pending_survives_restart_and_only_accepted_credit_reads_decide(
    observed: int, resets: bool, kind: str, pending: bool,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    provider = FakeReader()
    provider.read_result = LimitsSnapshot(Window(8, 99999, 300), None, ResetCredits(0, [], 0) if resets else None, observed)
    scheduler = make_scheduler(settings_factory(), provider)
    scheduler.states["codex"].weekly_resets = [event(credits_before=[CREDIT], dismissed=True), event(id="000000000002", credits_before=[CREDIT])]
    save(scheduler.path, scheduler.states)
    scheduler.states = load(scheduler.path, scheduler.settings, scheduler.providers)
    asyncio.run(scheduler.refresh_limits("codex"))
    assert len(scheduler.states["codex"].weekly_resets) == 2
    for entry in scheduler.states["codex"].weekly_resets:
        assert entry.kind == kind
        assert (entry.credits_before is not None) is pending


@pytest.mark.parametrize("known,refreshable,busy", [(False, True, False), (True, False, False), (True, True, True)])
def test_weekly_followup_only_with_known_credits_and_available_reader(
    known: bool, refreshable: bool, busy: bool,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        provider = FakeReader() if refreshable else FakeProvider("codex")
        provider.result = StokeResult(Window(2, 19000, 300), NEW_WEEK, 12)
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["codex"]
        state.resets = ResetCredits(1, [CREDIT], 900) if known else None
        state.refreshing = busy
        await scheduler.stoke_now("codex", "manual")
        await asyncio.sleep(0)
        assert not scheduler._attempts
        assert getattr(provider, "read_calls", 0) == 0
        assert state.weekly_resets[0].credits_before == ([CREDIT] if known else None)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [ProviderError("failed"), RuntimeError("private")])
def test_weekly_followup_errors_are_handled_and_leave_event_pending(
    failure: Exception, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        provider = FakeReader()
        provider.result = StokeResult(Window(2, 19000, 300), NEW_WEEK, 12)
        provider.read_result = failure
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["codex"]
        state.resets = ResetCredits(1, [CREDIT], 900)
        await scheduler.stoke_now("codex", "manual")
        await asyncio.gather(*scheduler._attempts)
        assert not scheduler._attempts and not state.refreshing
        assert state.weekly_resets[0].credits_before == [CREDIT]
        assert state.last_read is not None and not state.last_read.ok
        assert state.last_stoke.ok and state.consecutive_failures == 0
    asyncio.run(scenario())
    assert "refresh failed" in caplog.text and "private" not in caplog.text


def test_weekly_followup_is_cancelled_at_shutdown(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        provider = FakeReader()
        provider.result = StokeResult(Window(2, 19000, 300), NEW_WEEK, 12)
        provider.read_release = asyncio.Event()
        scheduler = make_scheduler(settings_factory(), provider)
        state = scheduler.states["codex"]
        state.resets = ResetCredits(1, [CREDIT], 900)
        await scheduler.stoke_now("codex", "manual")
        await provider.read_started.wait()
        tasks = set(scheduler._attempts)
        await scheduler.stop()
        assert tasks and all(task.cancelled() for task in tasks)
        assert not scheduler._attempts and not state.refreshing
        assert state.weekly_resets[0].credits_before == [CREDIT]
    asyncio.run(scenario())


def test_weekly_store_roundtrip_order_no_cap_and_legacy_file(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    state = scheduler.states["claude"]
    state.weekly_resets = [event(id=f"{index:012x}", kind="credit" if index % 2 else "early",
                                dismissed=bool(index % 3), credits_before=[CREDIT] if index % 2 else None)
                           for index in range(120)]
    save(scheduler.path, scheduler.states)
    assert load(scheduler.path, scheduler.settings, scheduler.providers) == scheduler.states
    data = json.loads(scheduler.path.read_text(encoding="utf-8"))
    assert data["claude"]["weekly_resets"] == [asdict(entry) for entry in state.weekly_resets]
    del data["claude"]["weekly_resets"]
    scheduler.path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"] == replace(state, weekly_resets=[])
    assert "using defaults" not in caplog.text


@pytest.mark.parametrize("field,value", [
    ("id", "ABCDEF012345"), ("id", "abcdef"), ("id", None),
    ("kind", "bad"), ("kind", []), ("kind", None),
    ("from_at", True), ("to_at", -1), ("before_resets_at", "40000"), ("after_resets_at", 1.5),
    ("before_used", True), ("before_used", -1), ("after_used", 101),
    ("after_used", float("nan")), ("after_used", float("inf")),
    ("dismissed", 1), ("dismissed", None),
    ("credits_before", {}), ("credits_before", [{}]),
    ("credits_before", [{"title": 1, "granted_at": 10, "expires_at": 5000}]),
    ("credits_before", [{"title": "Full reset", "expires_at": 5000}]),
    ("credits_before", [{"title": "Full reset", "granted_at": True, "expires_at": 5000}]),
    ("credits_before", [{"title": "Full reset", "granted_at": None, "expires_at": -1}]),
])
def test_weekly_invalid_event_uses_existing_corrupt_file_fallback(
    field: str, value: Any, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    save(scheduler.path, scheduler.states)
    data = json.loads(scheduler.path.read_text(encoding="utf-8"))
    data["claude"]["weekly_resets"] = [{**asdict(event()), field: value}]
    scheduler.path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    state = load(scheduler.path, scheduler.settings, scheduler.providers)["claude"]
    assert state.weekly_resets == [] and state.weekly is None
    assert "using defaults" in caplog.text


@pytest.mark.parametrize("value", [None, {}, "bad", [{}], [None]])
def test_weekly_invalid_history_container(
    value: Any, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    save(scheduler.path, scheduler.states)
    data = json.loads(scheduler.path.read_text(encoding="utf-8"))
    data["claude"]["weekly_resets"] = value
    scheduler.path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    assert load(scheduler.path, scheduler.settings, scheduler.providers)["claude"].weekly is None
    assert "using defaults" in caplog.text


def test_weekly_live_serialization_hides_internal_credits_without_mutating_state(settings_factory: Callable[..., Settings]) -> None:
    scheduler = make_scheduler(settings_factory(), FakeProvider())
    state = scheduler.states["claude"]
    state.weekly_resets = [event(credits_before=[CREDIT]), event(id="000000000001", kind="credit", dismissed=True)]
    before = deepcopy(state)
    live = serialize_tool(state, 1000, settings=scheduler.settings)
    assert all("credits_before" not in entry for entry in live["weekly_resets"])
    assert len(live["weekly_resets"]) == 2 and live["weekly_resets"][1]["dismissed"]
    assert serialize_tool(state)["weekly_resets"][0]["credits_before"] == [asdict(CREDIT)]
    assert state == before


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_weekly_api_dismiss_is_persistent_idempotent_and_shared(
    name: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        scheduler = make_scheduler(settings_factory(), FakeProvider(name))
        state = scheduler.states[name]
        state.weekly_resets = [event(credits_before=[CREDIT]), event(id="000000000001", kind="credit")]
        before = deepcopy(state)
        transport = httpx.ASGITransport(app=create_app(scheduler, scheduler.settings))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            url = f"/api/tools/{name}/weekly-resets/dismiss"
            for _ in range(2):
                response = await client.post(url, headers=HEADERS, json={"id": "abcdef012345"})
                assert response.status_code == 200
                assert response.json()["weekly_resets"][0]["dismissed"] is True
                assert "credits_before" not in response.json()["weekly_resets"][0]
                assert len(response.json()["weekly_resets"]) == 2
            assert (await client.get(url)).status_code == 405
            response = await client.post(url, headers=HEADERS, json={"id": "not-known"})
            assert response.status_code == 422 and response.json()["detail"] == "unknown weekly reset"
            assert (await client.post(url, json={"id": "abcdef012345"})).status_code == 403
            assert (await client.post("/api/tools/unknown/weekly-resets/dismiss", headers=HEADERS, json={"id": "abcdef012345"})).status_code == 404
            shared = (await client.get("/api/state")).json()["tools"][name]["weekly_resets"]
            assert shared[0]["dismissed"] and all("credits_before" not in entry for entry in shared)
        before.weekly_resets[0].dismissed = True
        assert state == before
        assert load(scheduler.path, scheduler.settings, scheduler.providers)[name] == state
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{}, {"id": 1}, {"id": None}, {"id": "x", "extra": True}, [], True, "id"])
def test_weekly_api_rejects_wrong_body_shape(body: Any, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        scheduler = make_scheduler(settings_factory(), FakeProvider())
        transport = httpx.ASGITransport(app=create_app(scheduler, scheduler.settings))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            url = "/api/tools/claude/weekly-resets/dismiss"
            assert (await client.post(url, headers=HEADERS, json=body)).status_code == 422
            assert (await client.post(url, headers=HEADERS, content="invalid")).status_code == 422
            assert (await client.post(url, headers=HEADERS)).status_code == 422
    asyncio.run(scenario())
