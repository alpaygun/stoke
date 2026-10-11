"""Persistence round trips, safe fallback, and atomic replacement."""

import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeProvider
from stoke.config import Settings
from stoke.models import StokeRecord, ToolState, Window, serialize_tool
from stoke.store import load, save


@pytest.mark.parametrize("limits_at", [None, 0, 1002])
def test_store_round_trip_and_running_not_persisted(
    limits_at: int | None, settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    state = ToolState("claude", "Claude", True, True, Window(17, 20000, 300), Window(45, 40000, 10080),
                      StokeRecord(1000, "manual", True, None, True, 42), 20090, 0, None, limits_at)
    path = settings.data_dir / "state.json"
    save(path, {"claude": state})
    data = json.loads(path.read_text())
    assert data == {"claude": serialize_tool(state)}
    assert "running" not in data["claude"] and "window_open" not in data["claude"]
    loaded = load(path, settings, {"claude": FakeProvider()})["claude"]
    state.running = False
    assert loaded == state


def test_v020_state_without_limits_at_preserves_all_values(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    path.parent.mkdir(parents=True)
    item = {
        "name": "claude", "label": "Claude", "auto_stoke": True,
        "five_hour": {"used_percent": 17, "resets_at": 20000, "window_minutes": 300},
        "weekly": {"used_percent": 45, "resets_at": 40000, "window_minutes": 10080},
        "last_stoke": {
            "at": 1000, "trigger": "manual", "ok": False, "error": "limits unreadable",
            "opened_new_window": None, "duration_ms": 42,
        },
        "next_stoke_at": 1060, "consecutive_failures": 1, "warning": "limits_unreadable",
    }
    data = {"claude": item, "codex": {**item, "name": "codex", "label": "Codex"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    states = load(path, settings, {"claude": FakeProvider(), "codex": FakeProvider("codex")})
    for name, state in states.items():
        assert state.limits_at is None
        assert not state.running
        assert serialize_tool(state) == {**data[name], "limits_at": None}
    assert "using defaults" not in caplog.text


@pytest.mark.parametrize("limits_at", ["1002", -1, True, 1.5, [], {}])
def test_invalid_limits_at_resets_all_tools_to_defaults(
    limits_at: object, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    states = {
        name: ToolState(name, provider.label, True, False, Window(17, 20000, 300), None,
                        StokeRecord(1000, "manual", True, None, None, 42), 20090, 0, None, 1002)
        for name, provider in {"claude": FakeProvider(), "codex": FakeProvider("codex")}.items()
    }
    save(path, states)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["codex"]["limits_at"] = limits_at
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load(path, settings, {"claude": FakeProvider(), "codex": FakeProvider("codex")})
    assert loaded == {
        name: ToolState(name, state.label, False, False, None, None, None, None, 0, None)
        for name, state in states.items()
    }
    assert all(state.limits_at is None for state in loaded.values())
    assert "using defaults" in caplog.text


@pytest.mark.parametrize("contents", [None, "{broken", "[]", '{"claude":{}}',
    '{"claude":{"warning":[]}}', '\ufeff{}'])
def test_missing_and_corrupt_file_falls_back(
    contents: str | None, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory(STOKE_AUTO_DEFAULT="1")
    path = settings.data_dir / "state.json"
    if contents is not None:
        path.parent.mkdir(parents=True)
        path.write_text(contents, encoding="utf-8")
    states = load(path, settings, {"claude": FakeProvider()})
    assert states["claude"] == ToolState("claude", "Claude", True, False, None, None, None, None, 0, None)
    assert "using defaults" in caplog.text
    assert contents is None or contents not in caplog.text


def test_new_tools_get_defaults_and_removed_tools_are_ignored(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    original = load(path, settings, {"claude": FakeProvider()})
    original["claude"].auto_stoke = True
    save(path, original)
    added = load(path, settings, {"claude": FakeProvider(), "codex": FakeProvider("codex")})
    assert added["claude"].auto_stoke and not added["codex"].auto_stoke
    removed = load(path, settings, {"codex": FakeProvider("codex")})
    assert list(removed) == ["codex"]


def test_atomic_failure_preserves_previous_file_and_cleans_temp(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    states = load(path, settings, {"claude": FakeProvider()})
    save(path, states)
    previous = path.read_bytes()
    def fail_replace(source: Path, destination: Path) -> None:
        assert Path(source).parent == destination.parent
        assert Path(source).is_file()
        raise OSError("synthetic replacement failure")
    monkeypatch.setattr("stoke.store.os.replace", fail_replace)
    states["claude"].auto_stoke = True
    with pytest.raises(OSError):
        save(path, states)
    assert path.read_bytes() == previous
    assert list(path.parent.iterdir()) == [path]
