"""Wiring and module entry point checks without starting a server."""

import runpy
import sys
from typing import Any, Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stoke import main
from stoke.config import Settings


def test_build_app_wires_enabled_tools_in_order(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(STOKE_TOOLS="codex,claude")
    app = main.build_app(settings)
    assert (settings.data_dir / "workdir").is_dir()
    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["dry_run"] and list(state["tools"]) == ["codex", "claude"]
        assert not any(tool["running"] for tool in state["tools"].values())


def test_run_uses_settings_and_stdout_logging(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(STOKE_HOST="127.0.0.1", STOKE_PORT="9876")
    logging_calls: list[dict[str, Any]] = []
    server_calls: list[tuple[FastAPI, dict[str, Any]]] = []
    def log_config(**kwargs: Any) -> None:
        logging_calls.append(kwargs)
    def server(app: FastAPI, **kwargs: Any) -> None:
        server_calls.append((app, kwargs))
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main.logging, "basicConfig", log_config)
    monkeypatch.setattr(main.uvicorn, "run", server)
    main.run()
    assert logging_calls[0]["stream"] is sys.stdout
    assert logging_calls[0]["level"] == main.logging.INFO
    assert server_calls[0][1] == {"host": "127.0.0.1", "port": 9876, "log_config": None}


def test_python_module_entry_point_calls_main_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(main, "run", lambda: calls.append(True))
    runpy.run_module("stoke.__main__", run_name="__main__")
    assert calls == [True]
