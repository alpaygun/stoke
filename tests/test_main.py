"""Wiring and module entry point checks without starting a server."""

import logging
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
        assert any(isinstance(item, main.HealthzAccessFilter) for item in access_logger.filters)
        server_calls.append((app, kwargs))
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main.logging, "basicConfig", log_config)
    monkeypatch.setattr(main.uvicorn, "run", server)
    access_logger = logging.getLogger("uvicorn.access")
    original_filters = access_logger.filters[:]
    try:
        main.run()
        attached = [item for item in access_logger.filters if item not in original_filters]
        assert len(attached) == 1 and isinstance(attached[0], main.HealthzAccessFilter)
    finally:
        for item in access_logger.filters[:]:
            if item not in original_filters:
                access_logger.removeFilter(item)
    assert logging_calls[0]["stream"] is sys.stdout
    assert logging_calls[0]["level"] == main.logging.INFO
    assert server_calls[0][1] == {"host": "127.0.0.1", "port": 9876, "log_config": None}


@pytest.mark.parametrize("path,keep", [
    ("/healthz", False), ("/healthz?x=1", False), ("/api/state", True),
    ("/api/tools/codex/stoke", True), ("/healthz/", True), ("/healthz-other", True),
])
def test_healthz_access_filter(path: str, keep: bool) -> None:
    args = ("127.0.0.1:1234", "POST" if "stoke" in path else "GET", path, "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %s", args, None)
    assert main.HealthzAccessFilter().filter(record) is keep


@pytest.mark.parametrize("args", [(), ("/healthz",), (1, 2, 3, 4, 5), {"path": "/healthz"}])
def test_healthz_access_filter_keeps_unexpected_args(args: Any) -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "unexpected", (), None)
    record.args = args
    assert main.HealthzAccessFilter().filter(record)


def test_python_module_entry_point_calls_main_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(main, "run", lambda: calls.append(True))
    runpy.run_module("stoke.__main__", run_name="__main__")
    assert calls == [True]
