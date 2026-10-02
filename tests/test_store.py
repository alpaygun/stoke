"""Persistence round trips, safe fallback, and atomic replacement."""

import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeProvider
from stoke.config import Settings
from stoke.models import StokeRecord, ToolState, Window, serialize_tool
from stoke.store import load, save


def test_store_round_trip_and_running_not_persisted(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    state = ToolState("claude", "Claude", True, True, Window(17, 20000, 300), Window(45, 40000, 10080),
                      StokeRecord(1000, "manual", True, None, True, 42), 20090, 0, None)
    path = settings.data_dir / "state.json"
    save(path, {"claude": state})
    data = json.loads(path.read_text())
    assert data == {"claude": serialize_tool(state)}
    assert "running" not in data["claude"] and "window_open" not in data["claude"]
    loaded = load(path, settings, {"claude": FakeProvider()})["claude"]
    state.running = False
    assert loaded == state


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
