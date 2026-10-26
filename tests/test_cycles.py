"""Cycle timing, retention, and isolation from the scheduling state."""

import asyncio
import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeProvider
from stoke.config import Settings
from stoke.cycles import CYCLE_RETENTION_SECONDS, CycleLog, CycleRecord
from stoke.main import build_app
from stoke.models import StokeResult, Window
from stoke.providers.base import LimitsUnreadable, ProviderError
from stoke.scheduler import Scheduler
from stoke.store import load
from fastapi.testclient import TestClient


def observation(at: int, tool: str = "codex") -> CycleRecord:
    return CycleRecord(tool, "auto", True, None, at, at + 5, 5000, 10, at - 10, at + 18000, True)


@pytest.mark.parametrize("outcome", ["success", "provider", "unreadable", "unexpected", "stale"])
def test_completed_attempt_cycle_content(
    outcome: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [1000]
    monkeypatch.setattr("stoke.clock.now", lambda: current[0])
    settings = settings_factory(STOKE_AUTO_DEFAULT="1", STOKE_TOOL_DELAYS="codex=10")
    result = {
        "success": StokeResult(Window(2, 19000, 300), None, 12),
        "provider": ProviderError("failed sk-ant-fixture-secret"),
        "unreadable": LimitsUnreadable("private details"),
        "unexpected": RuntimeError("private sk-ant-fixture-secret"),
        "stale": StokeResult(Window(2, 1001, 300), None, 12),
    }[outcome]
    provider = FakeProvider("codex", result)
    original = provider.stoke

    async def finish() -> StokeResult:
        current[0] = 1005
        return await original()

    monkeypatch.setattr(provider, "stoke", finish)
    providers = {"codex": provider}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    state = scheduler.states["codex"]
    state.five_hour = Window(40, 990, 300)
    asyncio.run(scheduler.stoke_now("codex", "auto"))
    record = state.last_stoke
    assert record is not None
    assert scheduler.cycles.read(1005) == [{
        "tool": "codex", "trigger": "auto", "ok": outcome == "success", "error": record.error,
        "attempt_at": 1000, "completed_at": 1005, "duration_ms": record.duration_ms,
        "delay_seconds": 10, "previous_end": 990,
        "new_end": 19000 if outcome == "success" else None,
        "opened": True if outcome == "success" else None,
    }]
    assert provider.calls == 1
    assert state.next_stoke_at == (19010 if outcome == "success" else 1065)
    stored = scheduler.path.read_text(encoding="utf-8")
    assert "cycles" not in stored and "delay_seconds" not in stored
    text = scheduler.cycles.path.read_text(encoding="utf-8")
    assert "sk-ant-" not in text and "private" not in text
    assert len(text.splitlines()) == 1


@pytest.mark.parametrize("end,closed,previous", [(None, None, None), (19000, 999, 999), (990, 900, None)])
def test_cycle_uses_relevant_previous_window(
    end: int | None, closed: int | None, previous: int | None,
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory()
    providers = {"codex": FakeProvider("codex")}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    state = scheduler.states["codex"]
    state.five_hour = None if end is None else Window(1, end, 300)
    state.window_closed_at = closed
    asyncio.run(scheduler.stoke_now("codex", "reset"))
    cycle, = scheduler.cycles.read(1000)
    assert cycle["previous_end"] == previous
    assert cycle["opened"] == state.last_stoke.opened_new_window


def test_cycle_retention_corruption_and_append(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log = CycleLog(tmp_path / "data" / "cycles.jsonl")
    now = CYCLE_RETENTION_SECONDS + 100
    assert log.read(now) == []
    log.prune(now)
    assert not log.path.exists()
    log.append(observation(99), 99)
    log.append(observation(100), 100)
    log.append(observation(now - 10), now - 10)
    with log.path.open("ab") as file:
        file.write(b'broken sk-ant-fixture-secret\n[]\n{"attempt_at": "bad"}\n\xff\n')
    assert [record["attempt_at"] for record in log.read(now)] == [now - 10, 100]
    log.read(now)
    assert len(caplog.records) == 1
    assert "sk-ant-" not in caplog.text
    log.prune(now)
    retained = [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines()]
    assert [record["attempt_at"] for record in retained] == [100, now - 10]
    unchanged = log.path.read_bytes()
    log.append(observation(now), now)
    assert log.path.read_bytes().startswith(unchanged)
    log.append(observation(now + 1), now + 1)
    assert [record["attempt_at"] for record in log.read(now + 1)] == [now + 1, now, now - 10]
    assert len(log.path.read_text(encoding="utf-8").splitlines()) == 3
    assert not list(log.path.parent.glob("*.tmp"))


def test_prune_replace_failure_keeps_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = CycleLog(tmp_path / "cycles.jsonl")
    log.append(observation(1), 1)
    before = log.path.read_bytes()

    def fail(*args: object) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr("stoke.cycles.os.replace", fail)
    with pytest.raises(OSError):
        log.prune(CYCLE_RETENTION_SECONDS + 2)
    assert log.path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [log.path]


@pytest.mark.parametrize("failure", ["path", "value"])
@pytest.mark.parametrize("ok", [True, False])
def test_cycle_logging_failure_is_harmless(
    failure: str, ok: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings = settings_factory(STOKE_AUTO_DEFAULT="1", STOKE_TOOL_DELAYS="codex=10")
    provider = FakeProvider("codex", None if ok else ProviderError("failed"))
    providers = {"codex": provider}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    if failure == "path":
        scheduler.cycles.path.mkdir(parents=True)  # A directory is never writable as a log file.
    else:
        def fail(*args: object) -> None:
            raise ValueError("private sk-ant-fixture-secret")
        monkeypatch.setattr(scheduler.cycles, "append", fail)
    asyncio.run(scheduler.stoke_now("codex", "manual"))
    state = scheduler.states["codex"]
    assert provider.calls == 1 and state.last_stoke.ok is ok
    assert not state.running and scheduler.events["codex"].is_set()
    assert state.next_stoke_at == (19010 if ok else 1060)
    assert load(scheduler.path, settings, providers)["codex"] == state
    assert "cycle logging failed" in caplog.text
    assert "private" not in caplog.text and "sk-ant-" not in caplog.text


def test_state_write_failure_prevents_provider_and_cycle(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory()
    provider = FakeProvider()
    providers = {"claude": provider}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)

    def fail() -> None:
        raise OSError("state unavailable")

    monkeypatch.setattr(scheduler, "_persist", fail)
    with pytest.raises(OSError):
        asyncio.run(scheduler.stoke_now("claude", "manual"))
    assert provider.calls == 0 and not scheduler.cycles.path.exists()
    assert not scheduler.states["claude"].running


@pytest.mark.parametrize("unavailable", [True, False])
def test_startup_pruning_and_failure_isolation(
    unavailable: bool, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = CYCLE_RETENTION_SECONDS + 100
    monkeypatch.setattr("stoke.clock.now", lambda: now)
    settings = settings_factory()
    providers = {"codex": FakeProvider("codex")}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    if unavailable:
        scheduler.cycles.path.mkdir(parents=True)
    else:
        scheduler.cycles.append(observation(99), 99)
        scheduler.cycles.append(observation(100), 100)

    async def scenario() -> None:
        await scheduler.start()
        assert scheduler.tasks and scheduler.path.exists()
        await scheduler.stop()
    asyncio.run(scenario())
    if not unavailable:
        assert [json.loads(line)["attempt_at"] for line in scheduler.cycles.path.read_text().splitlines()] == [100]


def test_dry_run_writes_cycle_records(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(STOKE_DRY_RUN="1", STOKE_TOOL_DELAYS="codex=10")
    with TestClient(build_app(settings)) as client:
        response = client.post("/api/tools/codex/stoke", headers={"X-Stoke": "1"})
        assert response.status_code == 200 and response.json()["last_stoke"]["ok"]
        cycle, = client.get("/api/cycles").json()["cycles"]
        assert cycle["tool"] == "codex" and cycle["ok"] and cycle["delay_seconds"] == 10
