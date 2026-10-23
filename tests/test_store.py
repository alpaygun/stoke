"""Persistence round trips, safe fallback, and atomic replacement."""

import json
from pathlib import Path
from typing import Callable

import pytest

from conftest import FakeProvider
from stoke.config import Settings
from stoke.models import Plan, PlanOutcome, ResetCredit, ResetCredits, StokeRecord, ToolState, Window, serialize_tool
from stoke.store import load, save


@pytest.mark.parametrize("limits_at", [None, 0, 1002])
@pytest.mark.parametrize("stale_resets", [0, 1, 4])
def test_store_round_trip_and_running_not_persisted(
    limits_at: int | None, stale_resets: int, settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    state = ToolState("claude", "Claude", True, True, Window(17, 20000, 300), Window(45, 40000, 10080),
                      StokeRecord(1000, "manual", True, None, True, 42), 20090, 0, None, limits_at, stale_resets)
    path = settings.data_dir / "state.json"
    save(path, {"claude": state})
    data = json.loads(path.read_text())
    assert data == {"claude": serialize_tool(state)}
    assert "running" not in data["claude"] and "window_open" not in data["claude"]
    loaded = load(path, settings, {"claude": FakeProvider()})["claude"]
    state.running = False
    assert loaded == state


def test_v020_state_without_limits_at_or_stale_resets_preserves_all_values(
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
        assert state.stale_resets == 0
        assert not state.running
        assert serialize_tool(state) == {**data[name], "limits_at": None, "stale_resets": 0, "resets": None, "plan": None, "plan_outcome": None, "auto_refresh": {"enabled": False, "seconds": 300}, "last_read": None, "manual_resets": [], "weekly_resets": []}
    assert "using defaults" not in caplog.text


@pytest.mark.parametrize("limits_at", ["1002", -1, True, 1.5, [], {}])
@pytest.mark.parametrize("field", ["limits_at", "stale_resets"])
def test_invalid_integer_field_resets_all_tools_to_defaults(
    limits_at: object, field: str, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
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
    data["codex"][field] = limits_at
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = load(path, settings, {"claude": FakeProvider(), "codex": FakeProvider("codex")})
    assert loaded == {
        name: ToolState(name, state.label, False, False, None, None, None, None, 0, None)
        for name, state in states.items()
    }
    assert all(state.limits_at is None for state in loaded.values())
    assert "using defaults" in caplog.text


def test_null_stale_resets_is_corrupt(settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"claude": FakeProvider()}
    states = load(path, settings, providers)
    states["claude"].auto_stoke = True
    save(path, states)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["claude"]["stale_resets"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    loaded = load(path, settings, providers)["claude"]
    assert not loaded.auto_stoke and loaded.stale_resets == 0
    assert "using defaults" in caplog.text


def test_auto_stopped_warning_loads(settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture) -> None:
    settings = settings_factory(STOKE_AUTO_DEFAULT="1")
    path = settings.data_dir / "state.json"
    state = ToolState("claude", "Claude", False, False, Window(17, 999, 300), None,
                      StokeRecord(1000, "auto", False, "reset time not in the future", None, 42),
                      None, 4, "auto_stopped", 900, 4)
    save(path, {"claude": state})
    assert load(path, settings, {"claude": FakeProvider()})["claude"] == state
    assert "using defaults" not in caplog.text


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


def test_reset_credits_round_trip_and_refreshing_is_live_only(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"codex": FakeProvider("codex")}
    states = load(path, settings, providers)
    state = states["codex"]
    state.resets = ResetCredits(2, [ResetCredit("Full reset", None, 2000), ResetCredit("Limit reset", 100, 3000)], 1000)
    state.refreshing = True
    state.running = True
    save(path, states)
    persisted = json.loads(path.read_text(encoding="utf-8"))["codex"]
    assert "refreshing" not in persisted and "running" not in persisted and "refreshable" not in persisted
    api = serialize_tool(state, 1000, refreshable=True, settings=settings)
    assert api["refreshing"] and api["running"] and api["refreshable"]
    state.refreshing = state.running = False
    assert load(path, settings, providers)["codex"] == state


def test_old_state_without_resets_preserves_live_values(settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"codex": FakeProvider("codex")}
    state = ToolState("codex", "Codex", True, False, Window(17, 20000, 300), Window(45, 40000, 10080),
                      StokeRecord(1000, "manual", True, None, True, 42), 20090, 2, "stoke_failing", 1002, 1)
    save(path, {"codex": state})
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["codex"]["resets"]
    path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    assert load(path, settings, providers)["codex"] == state
    assert "using defaults" not in caplog.text


def test_store_sanitizes_titles_and_sorts_loaded_credits(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"codex": FakeProvider("codex")}
    states = load(path, settings, providers)
    states["codex"].resets = ResetCredits(3, [
        ResetCredit(" \x00Full   reset\x7f ", 0, 3), ResetCredit("x" * 100, None, 2), ResetCredit("\t\n", 0, 1),
    ], 0)
    save(path, states)
    assert load(path, settings, providers)["codex"].resets == ResetCredits(3, [
        ResetCredit("Limit reset", 0, 1), ResetCredit("x" * 80, None, 2), ResetCredit("Full reset", 0, 3),
    ], 0)


@pytest.mark.parametrize("block", [[], "invalid", {},
    {"available": True, "credits": [], "read_at": 0},
    {"available": -1, "credits": [], "read_at": 0},
    {"available": "1", "credits": [], "read_at": 0},
    {"available": 1, "credits": [], "read_at": True},
    {"available": 1, "credits": [], "read_at": -1},
    {"available": 1, "credits": None, "read_at": 0},
    {"available": 1, "credits": [None], "read_at": 0},
    {"available": 1, "credits": [{"title": 1, "granted_at": None, "expires_at": 1}], "read_at": 0},
    {"available": 1, "credits": [{"title": "Reset", "granted_at": True, "expires_at": 1}], "read_at": 0},
    {"available": 1, "credits": [{"title": "Reset", "granted_at": -1, "expires_at": 1}], "read_at": 0},
    {"available": 1, "credits": [{"title": "Reset", "granted_at": None, "expires_at": True}], "read_at": 0},
    {"available": 1, "credits": [{"title": "Reset", "granted_at": None, "expires_at": -1}], "read_at": 0},
    {"available": 1, "credits": [{"title": "Reset", "expires_at": 1}], "read_at": 0},
])
def test_invalid_resets_take_corrupt_file_path(
    block: object, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"codex": FakeProvider("codex")}
    states = load(path, settings, providers)
    states["codex"].auto_stoke = True
    states["codex"].five_hour = Window(1, 20000, 300)
    save(path, states)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["codex"]["resets"] = block
    path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    loaded = load(path, settings, providers)["codex"]
    assert not loaded.auto_stoke and loaded.five_hour is None and loaded.resets is None
    assert "using defaults" in caplog.text


@pytest.mark.parametrize("repeat", ["once", "daily"])
@pytest.mark.parametrize("auto_after", [None, True, False])
@pytest.mark.parametrize("result", ["done", "missed", "broken"])
def test_plan_and_outcome_round_trip(
    repeat: str, auto_after: bool | None, result: str, settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"claude": FakeProvider()}
    state = ToolState("claude", "Claude", True, False, Window(1, 19000, 300), None,
                      StokeRecord(1000, "planned", True, None, True, 12), 19090, 0, None,
                      plan=Plan("07:00", repeat, auto_after, 20000, 20060),
                      plan_outcome=PlanOutcome(1000, result, 1001))
    save(path, {"claude": state})
    persisted = json.loads(path.read_text(encoding="utf-8"))["claude"]
    assert not {"upcoming_at", "upcoming_planned", "quiet_from"}.intersection(persisted)
    assert load(path, settings, providers)["claude"] == state
    api = serialize_tool(state, 20000, settings=settings)
    assert (api["upcoming_at"], api["upcoming_planned"], api["quiet_from"]) == (20060, True, 2000)


def test_old_state_without_plan_keys_preserves_both_tools(
    settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"claude": FakeProvider(), "codex": FakeProvider("codex")}
    states = load(path, settings, providers)
    for state in states.values():
        state.auto_stoke = True
        state.five_hour = Window(17, 20000, 300)
        state.last_stoke = StokeRecord(1000, "manual", False, "limits unreadable", None, 42)
        state.next_stoke_at = 20090
        state.limits_at = 900
        state.consecutive_failures = 2
    save(path, states)
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data.values():
        del item["plan"]
        del item["plan_outcome"]
    path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    assert load(path, settings, providers) == states
    assert all(state.plan is None and state.plan_outcome is None for state in states.values())
    assert "using defaults" not in caplog.text


@pytest.mark.parametrize("field,value", [
    ("plan", []), ("plan", "invalid"), ("plan", {}),
    ("plan_outcome", []), ("plan_outcome", "invalid"), ("plan_outcome", {}),
    ("time", "7:00"), ("time", "24:00"), ("time", "07:60"),
    ("time", "07:00\n"), ("time", "07:0١"), ("time", None), ("time", 700),
    ("repeat", "weekly"), ("repeat", []), ("repeat", None),
    ("auto_after", 1), ("auto_after", "true"), ("auto_after", []),
    ("target_at", True), ("target_at", -1), ("target_at", "1000"), ("target_at", 1.5),
    ("retry_at", True), ("retry_at", -1), ("retry_at", "1000"), ("retry_at", 1.5),
    ("result", "failed"), ("result", []), ("result", None),
    ("at", True), ("at", -1), ("at", "1000"), ("at", 1.5),
    ("outcome_target_at", True), ("outcome_target_at", -1), ("outcome_target_at", "1000"),
])
def test_invalid_plan_values_take_corrupt_file_path(
    field: str, value: object, settings_factory: Callable[..., Settings], caplog: pytest.LogCaptureFixture,
) -> None:
    settings = settings_factory()
    path = settings.data_dir / "state.json"
    providers = {"claude": FakeProvider(), "codex": FakeProvider("codex")}
    states = load(path, settings, providers)
    for state in states.values():
        state.auto_stoke = True
        state.plan = Plan("07:00", "daily", None, 20000, None)
        state.plan_outcome = PlanOutcome(1000, "done", 1001)
    save(path, states)
    data = json.loads(path.read_text(encoding="utf-8"))
    item = data["codex"]
    if field in {"plan", "plan_outcome"}:
        item[field] = value
    elif field in {"result", "at", "outcome_target_at"}:
        item["plan_outcome"]["target_at" if field == "outcome_target_at" else field] = value
    else:
        item["plan"][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    caplog.clear()
    loaded = load(path, settings, providers)
    assert all(not state.auto_stoke and state.plan is None and state.plan_outcome is None for state in loaded.values())
    assert "using defaults" in caplog.text
