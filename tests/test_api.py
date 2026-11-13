"""API contract, POST protection, persistence, and manual request concurrency."""

import asyncio
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider, FakeReader
from stoke.api import create_app
from stoke.config import Settings
from stoke.cycles import CYCLE_RETENTION_SECONDS, CycleRecord
from stoke.models import Window
from stoke.providers.base import LimitsUnreadable, ProviderError
from stoke.scheduler import Scheduler
from stoke.store import load

HEADERS = {"X-Stoke": "1"}


@pytest.mark.parametrize("name", ["claude", "codex"])
@pytest.mark.parametrize("action", ["resets", "resets/update", "resets/remove"])
def test_removed_reset_routes(name: str, action: str, settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        for method in ("get", "post"):
            result = getattr(client, method)(f"/api/tools/{name}/{action}", headers=HEADERS)
            assert result.status_code in {404, 405}


def test_manual_rate_limit_per_tool_and_rolling_expiry(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000]
    monkeypatch.setattr("stoke.clock.now", lambda: now[0])

    async def scenario() -> None:
        settings = settings_factory()
        providers = {"claude": FakeProvider(), "codex": FakeProvider("codex")}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/claude/stoke"
            for index in range(6):
                now[0] = 1000 + index * 10
                response = await client.post(url, headers=HEADERS)
                assert response.status_code == 200 and response.json()["last_stoke"]["ok"]
            response = await client.post(url, headers=HEADERS)
            assert response.status_code == 429
            assert response.json() == {
                "detail": "too many manual stokes: 6 per hour; try again in 60 min",
                "code": "rate_limited", "retry_at": 4600, "limit": 6,
            }
            assert response.headers["Retry-After"] == "3550"
            assert providers["claude"].calls == 6
            assert (await client.post("/api/tools/codex/stoke", headers=HEADERS)).status_code == 200
            assert providers["codex"].calls == 1
            for at, minutes, seconds in [(4539, 2, 61), (4599, 1, 1)]:
                now[0] = at
                response = await client.post(url, headers=HEADERS)
                assert response.status_code == 429
                assert response.json()["detail"].endswith(f"try again in {minutes} min")
                assert response.headers["Retry-After"] == str(seconds)
            now[0] = 4600
            assert (await client.post(url, headers=HEADERS)).status_code == 200
            assert providers["claude"].calls == 7
            response = await client.post(url, headers=HEADERS)
            assert response.status_code == 429 and response.json()["retry_at"] == 4610
    asyncio.run(scenario())


def test_manual_rate_limit_counts_failed_stokes(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeProvider(result=LimitsUnreadable("no limits"))
        scheduler = make_scheduler(settings, provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            for _ in range(6):
                response = await client.post("/api/tools/claude/stoke", headers=HEADERS)
                assert response.status_code == 200
                assert response.json()["last_stoke"]["ok"] is False
            response = await client.post("/api/tools/claude/stoke", headers=HEADERS)
            assert response.status_code == 429 and response.json()["code"] == "rate_limited"
            assert provider.calls == 6
    asyncio.run(scenario())


@pytest.mark.parametrize("rejection", ["plan_conflict", "already_running", "invalid_body"])
def test_manual_rate_limit_does_not_count_rejected_requests(
    rejection: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        scheduler = make_scheduler(settings)
        if rejection == "plan_conflict":
            scheduler.set_plan("claude", "07:00", "once", None)
        if rejection == "already_running":
            await scheduler.locks["claude"].acquire()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/claude/stoke"
            try:
                for _ in range(8):
                    if rejection == "invalid_body":
                        response = await client.post(url, headers=HEADERS, json={"break_plan": "yes"})
                        assert response.status_code == 422
                    else:
                        response = await client.post(url, headers=HEADERS)
                        assert response.status_code == 409
                        expected = {"detail": "stoke already running"} if rejection == "already_running" else {
                            "detail": "stoke would break the plan", "code": "plan_conflict", "plan_target_at": 18000,
                        }
                        assert response.json() == expected
            finally:
                if rejection == "already_running":
                    scheduler.locks["claude"].release()
            if rejection == "plan_conflict":
                scheduler.cancel_plan("claude")
            assert scheduler.providers["claude"].calls == 0
            for _ in range(6):
                assert (await client.post(url, headers=HEADERS)).status_code == 200
            assert (await client.post(url, headers=HEADERS)).status_code == 429
            assert scheduler.providers["claude"].calls == 6
    asyncio.run(scenario())


def test_refresh_rate_limit_per_tool_expiry_and_internal_refresh(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000]
    monkeypatch.setattr("stoke.clock.now", lambda: now[0])

    async def scenario() -> None:
        settings = settings_factory()
        providers = {"claude": FakeReader("claude"), "codex": FakeReader()}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        await scheduler.refresh_limits("codex")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/codex/refresh"
            for _ in range(30):
                assert (await client.post(url, headers=HEADERS)).status_code == 200
            response = await client.post(url, headers=HEADERS)
            assert response.status_code == 429
            assert response.json() == {
                "detail": "too many usage refreshes: 30 per hour; try again in 60 min",
                "code": "rate_limited", "retry_at": 4600, "limit": 30,
            }
            assert response.headers["Retry-After"] == "3600"
            assert providers["codex"].read_calls == 31
            assert (await client.post("/api/tools/claude/refresh", headers=HEADERS)).status_code == 200
            assert (await client.post("/api/tools/codex/stoke", headers=HEADERS)).status_code == 200
            await scheduler.refresh_limits("codex")
            assert providers["codex"].read_calls == 32
            assert (await client.post(url, headers=HEADERS)).status_code == 429
            now[0] = 4600
            assert (await client.post(url, headers=HEADERS)).status_code == 200
            assert providers["codex"].read_calls == 33
    asyncio.run(scenario())


def test_refresh_rate_limit_counts_provider_errors(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeReader("claude")
        provider.read_result = ProviderError("read failed")
        scheduler = make_scheduler(settings, provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            for _ in range(30):
                response = await client.post("/api/tools/claude/refresh", headers=HEADERS)
                assert response.status_code == 502 and response.json() == {"detail": "read failed"}
            response = await client.post("/api/tools/claude/refresh", headers=HEADERS)
            assert response.status_code == 429 and response.json()["code"] == "rate_limited"
            assert provider.read_calls == 30
    asyncio.run(scenario())


@pytest.mark.parametrize("rejection", ["unsupported", "running", "refreshing"])
def test_refresh_rate_limit_does_not_count_rejected_requests(
    rejection: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeReader("claude")
        scheduler = make_scheduler(settings, provider)
        if rejection == "unsupported":
            scheduler.readers.pop("claude")
        else:
            setattr(scheduler.states["claude"], rejection, True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/claude/refresh"
            for _ in range(35):
                response = await client.post(url, headers=HEADERS)
                assert response.status_code == (404 if rejection == "unsupported" else 409)
                assert response.json() == {"detail": "refresh not supported" if rejection == "unsupported" else "already running"}
            assert provider.read_calls == 0
            if rejection == "unsupported":
                scheduler.readers["claude"] = provider
            else:
                setattr(scheduler.states["claude"], rejection, False)
            for _ in range(30):
                assert (await client.post(url, headers=HEADERS)).status_code == 200
            assert (await client.post(url, headers=HEADERS)).status_code == 429
            assert provider.read_calls == 30
    asyncio.run(scenario())


def test_manual_rate_limit_leaves_automatic_and_planned_stokes_unlimited(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        scheduler = make_scheduler(settings)
        await scheduler.stoke_now("claude", "auto")
        await scheduler.stoke_now("claude", "planned")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            for _ in range(6):
                assert (await client.post("/api/tools/claude/stoke", headers=HEADERS)).status_code == 200
            assert (await client.post("/api/tools/claude/stoke", headers=HEADERS)).status_code == 429
            state = await scheduler.stoke_now("claude", "auto")
            assert state.last_stoke is not None and state.last_stoke.trigger == "auto" and state.last_stoke.ok
            state = await scheduler.stoke_now("claude", "planned")
            assert state.last_stoke is not None and state.last_stoke.trigger == "planned" and state.last_stoke.ok
            assert scheduler.providers["claude"].calls == 10
            assert (await client.post("/api/tools/claude/stoke", headers=HEADERS)).status_code == 429
    asyncio.run(scenario())


@pytest.mark.parametrize(("action", "limit"), [("stoke", 6), ("refresh", 30)])
def test_rate_limit_instances_are_per_app(
    action: str, limit: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        scheduler = make_scheduler(settings, FakeReader("claude"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = f"/api/tools/claude/{action}"
            for _ in range(limit):
                assert (await client.post(url, headers=HEADERS)).status_code == 200
            assert (await client.post(url, headers=HEADERS)).status_code == 429
            other_action = "refresh" if action == "stoke" else "stoke"
            assert (await client.post(f"/api/tools/claude/{other_action}", headers=HEADERS)).status_code == 200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            assert (await client.post(url, headers=HEADERS)).status_code == 200
    asyncio.run(scenario())


@pytest.mark.parametrize(("action", "limit"), [("stoke", 6), ("refresh", 30)])
def test_rate_limit_reserves_slot_before_provider_await(
    action: str, limit: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeReader("claude")
        scheduler = make_scheduler(settings, provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = f"/api/tools/claude/{action}"
            for _ in range(limit - 1):
                assert (await client.post(url, headers=HEADERS)).status_code == 200
            provider.started.clear()
            provider.read_started.clear()
            provider.release = provider.read_release = asyncio.Event()
            pending = asyncio.create_task(client.post(url, headers=HEADERS))
            try:
                await (provider.started if action == "stoke" else provider.read_started).wait()
                response = await client.post(url, headers=HEADERS)
                assert response.status_code == 429 and response.json()["code"] == "rate_limited"
                assert (provider.calls if action == "stoke" else provider.read_calls) == limit
            finally:
                provider.release.set()
                result = await pending
            assert result.status_code == 200
    asyncio.run(scenario())


@pytest.mark.parametrize("minutes", [0, 30, 300])
def test_early_reset_api_setting_and_closed_window(
    minutes: int, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        providers = {"codex": FakeReader(), "claude": FakeProvider()}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        state = scheduler.states["codex"]
        state.five_hour, state.window_closed_at = Window(1, 2800, 300), 1000
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/codex/reset-hold"
            response = await client.post(url, headers=HEADERS, json={"minutes": minutes})
            assert response.status_code == 200
            tool = response.json()
            assert tool["reset_hold_minutes"] == minutes and tool["window_closed_at"] == 1000
            assert tool["reset_closes_window"] is True
            assert not tool["window_open"] and tool["five_hour"]["resets_at"] == 2800
            assert (await client.get("/api/state")).json()["tools"]["codex"] == tool
            assert load(scheduler.path, settings, providers)["codex"].reset_hold_minutes == minutes
            assert (await client.get(url)).status_code == 405
            assert (await client.post(url, json={"minutes": minutes})).status_code == 403
            for name, detail in [("unknown", "unknown tool"), ("claude", "refresh not supported")]:
                response = await client.post(f"/api/tools/{name}/reset-hold", headers=HEADERS, json={"minutes": minutes})
                assert response.status_code == 404 and response.json()["detail"] == detail
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [{"minutes": 30}, {}])
def test_reset_provider_capability_api_rejects_claude_hold(
    body: object, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)

    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeReader("claude")
        providers = {"claude": provider}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        state = scheduler.states["claude"]
        state.five_hour, state.next_stoke_at, state.auto_stoke = Window(40, 19000, 300), 19090, True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            before = (await client.get("/api/state")).json()["tools"]["claude"]
            assert before["refreshable"] and before["reset_closes_window"] is False and before["window_open"]
            response = await client.post("/api/tools/claude/reset-hold", headers=HEADERS, json=body)
            assert response.status_code == 404 and response.json()["detail"] == "refresh not supported"
            assert (await client.get("/api/state")).json()["tools"]["claude"] == before
        assert not scheduler.path.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"minutes": True}, {"minutes": False}, {"minutes": -1}, {"minutes": 301},
    {"minutes": "30"}, {"minutes": 30.0}, {"minutes": None}, {},
    {"minutes": 30, "extra": True}, [], True, None,
])
def test_early_reset_api_rejects_invalid_body(body: object, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        providers = {"codex": FakeReader()}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/codex/reset-hold"
            assert (await client.post(url, headers=HEADERS, json=body)).status_code == 422
            assert (await client.post(url, headers=HEADERS, content="invalid")).status_code == 422
            assert (await client.post(url, headers=HEADERS)).status_code == 422
        assert scheduler.states["codex"].reset_hold_minutes == 30
        assert not scheduler.path.exists()
    asyncio.run(scenario())


def test_show_scoped_api_defaults_persistence_and_post_protection(settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        providers = {"claude": FakeReader("claude"), "codex": FakeProvider("codex")}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            tools = (await client.get("/api/state")).json()["tools"]
            assert all(tool["show_scoped"] is True for tool in tools.values())
            url = "/api/tools/claude/show-scoped"
            assert (await client.post(url, json={"enabled": False})).status_code == 403
            assert not scheduler.path.exists()
            assert (await client.get(url)).status_code == 405
            # Visibility changes have no request quota and never read usage.
            for enabled in [False, True] * 16 + [False]:
                response = await client.post(url, headers=HEADERS, json={"enabled": enabled})
                assert response.status_code == 200 and response.json()["show_scoped"] is enabled
            tools = (await client.get("/api/state")).json()["tools"]
            assert tools["claude"] == response.json()
            assert tools["codex"]["show_scoped"] is True
            restored = load(scheduler.path, settings, providers)
            assert restored["claude"].show_scoped is False and restored["codex"].show_scoped is True
            assert providers["claude"].read_calls == 0 and providers["claude"].calls == 0
            for name, detail in [("unknown", "unknown tool"), ("codex", "refresh not supported")]:
                response = await client.post(f"/api/tools/{name}/show-scoped", headers=HEADERS, json={"enabled": False})
                assert response.status_code == 404 and response.json() == {"detail": detail}
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"enabled": 1}, {"enabled": 0}, {"enabled": 1.0}, {"enabled": "false"}, {"enabled": None},
    {"enabled": []}, {"enabled": {}}, {"enabled": False, "extra": True}, {}, [], True, None,
])
def test_show_scoped_api_rejects_invalid_body(body: object, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        scheduler = make_scheduler(settings, FakeReader("claude"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            url = "/api/tools/claude/show-scoped"
            assert (await client.post(url, headers=HEADERS, json=body)).status_code == 422
            assert (await client.post(url, headers=HEADERS, content="invalid")).status_code == 422
            assert (await client.post(url, headers=HEADERS)).status_code == 422
        assert scheduler.states["claude"].show_scoped is True
        assert not scheduler.path.exists()
    asyncio.run(scenario())


def make_scheduler(settings: Settings, provider: FakeProvider | None = None) -> Scheduler:
    """Build one isolated tool for API tests."""
    actual = provider if provider is not None else FakeProvider()
    tools = {"claude": actual}
    return Scheduler(tools, load(settings.data_dir / "state.json", settings, tools), settings)


def test_state_shape_manual_result_health_and_page(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    with TestClient(create_app(scheduler, settings)) as client:
        assert len(scheduler.tasks) == 1
        response = client.get("/api/state")
        assert response.status_code == 200
        state = response.json()
        assert set(state) == {"dry_run", "server_time", "timezone", "delay_seconds", "tools"}
        assert state["dry_run"] is True and state["server_time"] == 1000
        assert state["timezone"] == "Europe/Istanbul"
        tool = state["tools"]["claude"]
        assert tool == {
            "name": "claude", "label": "Claude", "auto_stoke": False, "running": False,
            "window_open": False, "five_hour": None, "weekly": None, "last_stoke": None,
            "next_stoke_at": None, "consecutive_failures": 0, "warning": None,
            "limits_at": None, "stale_resets": 0,
            "window_closed_at": None, "reset_hold_minutes": 30, "read_mode": "direct",
            "refreshable": False, "refreshing": False, "resets": None,
            "reset_closes_window": False, "delay_seconds": 90,
            "plan": None, "plan_outcome": None, "upcoming_at": None,
            "upcoming_planned": False, "quiet_from": None,
            "auto_refresh": {"enabled": False, "seconds": 600}, "last_read": None,
            "scoped_limits": [], "show_scoped": True, "plan_name": None, "weekly_resets": [],
            "auth": {"stoke": {"label": "1-year token", "expires_at": None}, "read": {"label": "Account login", "expires_at": None}},
        }
        result = client.post("/api/tools/claude/stoke", headers=HEADERS).json()
        assert result["five_hour"] == {"used_percent": 2.0, "resets_at": 19000, "window_minutes": 300}
        assert result["last_stoke"] == {
            "at": 1000, "trigger": "manual", "ok": True, "error": None,
            "opened_new_window": None, "duration_ms": 12,
        }
        assert result["window_open"] and not result["running"] and result["next_stoke_at"] is None
        assert result["limits_at"] == 1000
        assert client.get("/api/state").json()["tools"]["claude"]["limits_at"] == 1000
        assert client.get("/healthz").json() == {"ok": True}
        page = client.get("/")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert "DRY RUN" in page.text and "setInterval(refresh, 5000)" in page.text
    assert not scheduler.tasks


def test_state_exposes_configured_delay_seconds(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(STOKE_DELAY_SECONDS="137")
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        assert response.json()["delay_seconds"] == 137


def test_state_exposes_live_per_tool_delays(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(STOKE_DELAY_SECONDS="137", STOKE_TOOL_DELAYS="codex=10")
    providers = {"claude": FakeProvider(), "codex": FakeProvider("codex")}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    with TestClient(create_app(scheduler, settings)) as client:
        state = client.get("/api/state").json()
        assert state["delay_seconds"] == 137
        assert state["tools"]["claude"]["delay_seconds"] == 137
        assert state["tools"]["codex"]["delay_seconds"] == 10
        assert "cycles" not in state
        assert "delay_seconds" not in scheduler.path.read_text(encoding="utf-8")


def test_cycles_api_filters_validation_and_order(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = CYCLE_RETENTION_SECONDS + 100
    monkeypatch.setattr("stoke.clock.now", lambda: now)
    settings = settings_factory()
    providers = {"claude": FakeProvider(), "codex": FakeProvider("codex")}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    # Deliberately append out of timestamp order to test sorting rather than reversal.
    for tool, at in [("codex", 100), ("claude", now), ("codex", now - 86400), ("claude", now - 100)]:
        scheduler.cycles.append(CycleRecord(tool, "auto", True, None, at, at + 1, 1000, 90, None, at + 18000, None), now)
    with TestClient(create_app(scheduler, settings)) as client:
        response = client.get("/api/cycles")  # No X-Stoke header.
        assert response.status_code == 200 and set(response.json()) == {"cycles"}
        records = response.json()["cycles"]
        assert [record["attempt_at"] for record in records] == [now, now - 100, now - 86400, 100]
        assert client.get("/api/cycles?tool=codex").json()["cycles"] == records[2:]
        assert client.get("/api/cycles?days=1").json()["cycles"] == records[:3]
        assert client.get("/api/cycles?days=1&tool=claude").json()["cycles"] == records[:2]
        for query in ["days=0", "days=31", "days=-1", "days=1.5", "days=abc", "days="]:
            assert client.get(f"/api/cycles?{query}").status_code == 422
        assert client.get("/api/cycles?tool=other").status_code == 404
        assert client.post("/api/cycles", headers=HEADERS).status_code == 405
    assert not scheduler.states["claude"].last_stoke and not scheduler.states["codex"].last_stoke


def test_cycles_api_missing_and_unavailable_file(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    with TestClient(create_app(scheduler, settings)) as client:
        assert client.get("/api/cycles").json() == {"cycles": []}
        assert client.get("/api/cycles?tool=codex").status_code == 404
        scheduler.cycles.path.mkdir()
        response = client.get("/api/cycles")
        assert response.status_code == 503 and response.json() == {"detail": "cycle log unavailable"}


@pytest.mark.parametrize("path", ["/api/tools/claude/stoke", "/api/tools/claude/auto", "/api/tools/claude/refresh", "/api/tools/claude/plan", "/api/tools/claude/plan/cancel", "/api/tools/claude/resets/update", "/unknown"])
@pytest.mark.parametrize("headers", [{}, {"X-Stoke": "0"}])
def test_every_post_requires_header(path: str, headers: dict[str, str], settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        assert client.post(path, headers=headers, json={"enabled": False}).status_code == 403


@pytest.mark.parametrize("action", ["stoke", "auto", "refresh", "plan", "plan/cancel", "resets/update"])
def test_unknown_tool_is_404(action: str, settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        assert client.post(f"/api/tools/unknown/{action}", headers=HEADERS, json={"enabled": True}).status_code == 404


@pytest.mark.parametrize("body", [{}, {"enabled": 1}, {"enabled": "true"}, {"enabled": None}, [], True])
def test_auto_body_requires_actual_bool(body: object, settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        assert client.post("/api/tools/claude/auto", headers=HEADERS, json=body).status_code == 422
        assert client.post("/api/tools/claude/auto", headers=HEADERS, content="invalid").status_code == 422


def test_auto_toggle_persists_and_cancels_pending_stoke(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    scheduler.states["claude"].five_hour = Window(1, 2000, 300)
    with TestClient(create_app(scheduler, settings)) as client:
        enabled = client.post("/api/tools/claude/auto", headers=HEADERS, json={"enabled": True}).json()
        assert enabled["auto_stoke"] is True and enabled["next_stoke_at"] == 2090
        saved = load(scheduler.path, settings, scheduler.providers)["claude"]
        assert saved.auto_stoke and saved.next_stoke_at == 2090
        disabled = client.post("/api/tools/claude/auto", headers=HEADERS, json={"enabled": False}).json()
        assert not disabled["auto_stoke"] and disabled["next_stoke_at"] is None
        saved = load(scheduler.path, settings, scheduler.providers)["claude"]
        assert not saved.auto_stoke and saved.next_stoke_at is None


def test_manual_request_waits_and_overlap_returns_409(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeProvider()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings, provider)
        app = create_app(scheduler, settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                manual = asyncio.create_task(client.post("/api/tools/claude/stoke", headers=HEADERS))
                await provider.started.wait()
                assert not manual.done()
                state = (await client.get("/api/state")).json()["tools"]["claude"]
                assert state["running"] is True
                conflict = await client.post("/api/tools/claude/stoke", headers=HEADERS)
                assert conflict.status_code == 409 and provider.calls == 1
                provider.release.set()
                result = await manual
                assert result.status_code == 200
                assert not result.json()["running"] and result.json()["last_stoke"]["ok"]
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_failed_manual_stoke_returns_state_and_warning(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    scheduler = make_scheduler(settings, FakeProvider(result=LimitsUnreadable("no limits")))
    with TestClient(create_app(scheduler, settings)) as client:
        response = client.post("/api/tools/claude/stoke", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["last_stoke"]["error"] == "limits unreadable"
        assert response.json()["warning"] == "limits_unreadable"


def test_refresh_api_state_success_and_all_response_capabilities(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        providers = {"codex": FakeReader(), "claude": FakeProvider()}
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        app = create_app(scheduler, settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            before = (await client.get("/api/state")).json()["tools"]
            assert before["codex"]["refreshable"] and not before["claude"]["refreshable"]
            assert not before["codex"]["refreshing"] and before["codex"]["resets"] is None
            response = await client.post("/api/tools/codex/refresh", headers=HEADERS)
            assert response.status_code == 200
            result = response.json()
            assert result["refreshable"] and not result["refreshing"] and result["five_hour"] is None
            assert result["limits_at"] == 1000
            assert result["resets"] == {"available": 1, "credits": [{"title": "Full reset", "granted_at": 10, "expires_at": 700000, "usable": None}], "read_at": 1000, "cooldown_until": None}
            assert (await client.get("/api/state")).json()["tools"]["codex"] == result
            for action, body in [("stoke", None), ("auto", {"enabled": False})]:
                response = await client.post(f"/api/tools/codex/{action}", headers=HEADERS, json=body)
                assert response.status_code == 200 and response.json()["refreshable"]
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    asyncio.run(scenario())


def test_refresh_unsupported_returns_exact_detail(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        response = client.post("/api/tools/claude/refresh", headers=HEADERS)
        assert response.status_code == 404 and response.json() == {"detail": "refresh not supported"}
        unknown = client.post("/api/tools/unknown/refresh", headers=HEADERS)
        assert unknown.status_code == 404 and unknown.json() == {"detail": "unknown tool"}


@pytest.mark.parametrize("failure", [ProviderError("read denied sk-ant-fixture-secret"), LimitsUnreadable("limits unreadable"), ValueError("private text")])
def test_refresh_failure_502_safe_detail(failure: Exception, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeReader()
        provider.read_result = failure
        tools = {"codex": provider}
        scheduler = Scheduler(tools, load(settings.data_dir / "state.json", settings, tools), settings)
        app = create_app(scheduler, settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/tools/codex/refresh", headers=HEADERS)
            assert response.status_code == 502
            expected = str(failure) if isinstance(failure, ProviderError) else "unexpected provider error"
            assert response.json() == {"detail": expected} and "sk-ant-" not in response.text
            assert not scheduler.states["codex"].refreshing
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["refresh", "stoke"])
def test_refresh_409_and_live_state_during_operation(operation: str, settings_factory: Callable[..., Settings]) -> None:
    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeReader()
        provider.read_release = provider.release = asyncio.Event()
        tools = {"codex": provider}
        scheduler = Scheduler(tools, load(settings.data_dir / "state.json", settings, tools), settings)
        app = create_app(scheduler, settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post(f"/api/tools/codex/{operation}", headers=HEADERS))
            await (provider.read_started if operation == "refresh" else provider.started).wait()
            state = (await client.get("/api/state")).json()["tools"]["codex"]
            assert state["refreshable"] and state["refreshing"] is (operation == "refresh")
            response = await client.post("/api/tools/codex/refresh", headers=HEADERS)
            assert response.status_code == 409 and response.json() == {"detail": "already running"}
            provider.release.set()
            assert (await first).status_code == 200
    asyncio.run(scenario())


@pytest.mark.parametrize("repeat", ["once", "daily"])
@pytest.mark.parametrize("auto_after", [None, True, False])
def test_plan_set_state_and_cancel(
    repeat: str, auto_after: bool | None, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    with TestClient(create_app(scheduler, settings)) as client:
        response = client.post("/api/tools/claude/plan", headers=HEADERS, json={"time": "07:00", "repeat": repeat, "auto_after": auto_after})
        assert response.status_code == 200
        tool = response.json()
        assert tool["plan"] == {"time": "07:00", "repeat": repeat, "auto_after": auto_after, "target_at": 18000, "retry_at": None}
        assert tool["upcoming_at"] == 18000 and tool["upcoming_planned"] is True
        assert tool["quiet_from"] == 0 and tool["next_stoke_at"] is None
        assert load(scheduler.path, settings, scheduler.providers)["claude"].plan == scheduler.states["claude"].plan
        state = client.get("/api/state").json()
        assert state["timezone"] == settings.timezone.key
        assert state["tools"]["claude"] == tool
        cancelled = client.post("/api/tools/claude/plan/cancel", headers=HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["plan"] is None and cancelled.json()["quiet_from"] is None
        assert cancelled.json()["upcoming_at"] is None and not cancelled.json()["upcoming_planned"]
        assert load(scheduler.path, settings, scheduler.providers)["claude"].plan is None
        assert client.post("/api/tools/claude/plan/cancel", headers=HEADERS).status_code == 200


@pytest.mark.parametrize("body", [
    {}, [], True, None,
    {"time": "07:00", "repeat": "daily"},
    {"time": "7:00", "repeat": "daily", "auto_after": None},
    {"time": "24:00", "repeat": "daily", "auto_after": None},
    {"time": "07:60", "repeat": "daily", "auto_after": None},
    {"time": "07:00\n", "repeat": "daily", "auto_after": None},
    {"time": 700, "repeat": "daily", "auto_after": None},
    {"time": "07:00", "repeat": "weekly", "auto_after": None},
    {"time": "07:00", "repeat": [], "auto_after": None},
    {"time": "07:00", "repeat": "daily", "auto_after": 1},
    {"time": "07:00", "repeat": "daily", "auto_after": "true"},
    {"time": "07:00", "repeat": "daily", "auto_after": None, "extra": True},
])
def test_plan_strict_body_validation(body: object, settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    with TestClient(create_app(scheduler, settings)) as client:
        assert client.post("/api/tools/claude/plan", headers=HEADERS, json=body).status_code == 422
        assert client.post("/api/tools/claude/plan", headers=HEADERS, content="invalid").status_code == 422
        assert scheduler.states["claude"].plan is None


def test_plan_unreachable_response(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    previous = scheduler.set_plan("claude", "08:00", "daily", None).plan
    scheduler.states["claude"].five_hour = Window(1, 19500, 300)
    with TestClient(create_app(scheduler, settings)) as client:
        response = client.post("/api/tools/claude/plan", headers=HEADERS, json={"time": "07:00", "repeat": "once", "auto_after": True})
        assert response.status_code == 422
        assert response.json() == {"detail": "window open until 07:25, so 07:00 cannot be reached"}
        assert scheduler.states["claude"].plan == previous
        assert client.get("/api/state").json()["tools"]["claude"]["plan"]["time"] == "08:00"


def test_stoke_plan_conflict_body_and_break(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    scheduler = make_scheduler(settings)
    scheduler.set_plan("claude", "07:00", "once", True)
    with TestClient(create_app(scheduler, settings)) as client:
        for body in [None, {}, {"break_plan": False}]:
            response = client.post("/api/tools/claude/stoke", headers=HEADERS, json=body)
            assert response.status_code == 409
            assert response.json() == {"detail": "stoke would break the plan", "code": "plan_conflict", "plan_target_at": 18000}
            assert scheduler.providers["claude"].calls == 0
        response = client.post("/api/tools/claude/stoke", headers=HEADERS, json={"break_plan": True})
        assert response.status_code == 200 and response.json()["last_stoke"]["ok"]
        assert response.json()["plan"] is None and not response.json()["auto_stoke"]
        assert response.json()["plan_outcome"] == {"target_at": 18000, "result": "broken", "at": 1000}


@pytest.mark.parametrize("body", [None, {}, {"break_plan": False}, {"break_plan": True}])
def test_stoke_optional_body_without_plan(body: object, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        response = client.post("/api/tools/claude/stoke", headers=HEADERS, json=body)
        assert response.status_code == 200 and response.json()["last_stoke"]["ok"]
        assert client.post("/api/tools/claude/stoke", headers=HEADERS, content="").status_code == 200


@pytest.mark.parametrize("body", [{"break_plan": 1}, {"break_plan": "true"}, {"break_plan": None}, [], True])
def test_stoke_break_requires_bool(body: object, settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        assert client.post("/api/tools/claude/stoke", headers=HEADERS, json=body).status_code == 422


def test_running_conflict_stays_unchanged_with_plan(settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    async def scenario() -> None:
        settings = settings_factory()
        provider = FakeProvider()
        provider.release = asyncio.Event()
        scheduler = make_scheduler(settings, provider)
        app = create_app(scheduler, settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = asyncio.create_task(client.post("/api/tools/claude/stoke", headers=HEADERS))
            await provider.started.wait()
            scheduler.set_plan("claude", "07:00", "daily", None)
            response = await client.post("/api/tools/claude/stoke", headers=HEADERS, json={"break_plan": True})
            assert response.status_code == 409 and response.json() == {"detail": "stoke already running"}
            provider.release.set()
            await task
            assert scheduler.states["claude"].plan_outcome is None
    asyncio.run(scenario())
