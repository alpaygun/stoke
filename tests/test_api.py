"""API contract, POST protection, persistence, and manual request concurrency."""

import asyncio
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FakeProvider
from stoke.api import create_app
from stoke.config import Settings
from stoke.models import Window
from stoke.providers.base import LimitsUnreadable
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
        assert set(state) == {"dry_run", "server_time", "tools"}
        assert state["dry_run"] is True and state["server_time"] == 1000
        tool = state["tools"]["claude"]
        assert tool == {
            "name": "claude", "label": "Claude", "auto_stoke": False, "running": False,
            "window_open": False, "five_hour": None, "weekly": None, "last_stoke": None,
            "next_stoke_at": None, "consecutive_failures": 0, "warning": None,
            "limits_at": None, "stale_resets": 0,
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


@pytest.mark.parametrize("path", ["/api/tools/claude/stoke", "/api/tools/claude/auto", "/unknown"])
@pytest.mark.parametrize("headers", [{}, {"X-Stoke": "0"}])
def test_every_post_requires_header(path: str, headers: dict[str, str], settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    with TestClient(create_app(make_scheduler(settings), settings)) as client:
        assert client.post(path, headers=headers, json={"enabled": False}).status_code == 403


@pytest.mark.parametrize("action", ["stoke", "auto"])
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
