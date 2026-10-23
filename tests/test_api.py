"""API contract, POST protection, persistence, and manual request concurrency."""

import asyncio
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider, FakeReader
from stoke.api import create_app
from stoke.config import Settings
from stoke.models import Window
from stoke.providers.base import LimitsUnreadable, ProviderError
from stoke.scheduler import Scheduler
from stoke.store import load

HEADERS = {"X-Stoke": "1"}


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
        assert set(state) == {"dry_run", "server_time", "timezone", "tools"}
        assert state["dry_run"] is True and state["server_time"] == 1000
        assert state["timezone"] == "Europe/Istanbul"
        tool = state["tools"]["claude"]
        assert tool == {
            "name": "claude", "label": "Claude", "auto_stoke": False, "running": False,
            "window_open": False, "five_hour": None, "weekly": None, "last_stoke": None,
            "next_stoke_at": None, "consecutive_failures": 0, "warning": None,
            "limits_at": None, "stale_resets": 0,
            "refreshable": False, "refreshing": False, "resets": None,
            "plan": None, "plan_outcome": None, "upcoming_at": None,
            "upcoming_planned": False, "quiet_from": None,
            "auto_refresh": {"enabled": False, "seconds": 300}, "last_read": None,
            "manual_resets": [], "manual_resets_allowed": True, "weekly_resets": [],
            "auth": {"stoke": {"label": "1-year token", "expires_at": None}, "read": {"label": "Browser login on the Pi"}},
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


@pytest.mark.parametrize("path", ["/api/tools/claude/stoke", "/api/tools/claude/auto", "/api/tools/claude/refresh", "/api/tools/claude/plan", "/api/tools/claude/plan/cancel", "/unknown"])
@pytest.mark.parametrize("headers", [{}, {"X-Stoke": "0"}])
def test_every_post_requires_header(path: str, headers: dict[str, str], settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        assert client.post(path, headers=headers, json={"enabled": False}).status_code == 403


@pytest.mark.parametrize("action", ["stoke", "auto", "refresh", "plan", "plan/cancel"])
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
            assert result["resets"] == {"available": 1, "credits": [{"title": "Full reset", "granted_at": 10, "expires_at": 700000}], "read_at": 1000}
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
