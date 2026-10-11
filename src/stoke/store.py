"""Validated JSON state loading and atomic persistence."""

import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .config import Settings
from .models import StokeRecord, ToolState, Window, serialize_tool
from .providers.base import Provider, short_error

logger = logging.getLogger(__name__)


def _int(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("invalid integer")
    return value


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid boolean")
    return value


def _window(value: Any) -> Window | None:
    if value is None:
        return None
    percent = value["used_percent"]
    if type(percent) not in {float, int} or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError("invalid percentage")
    minutes = value["window_minutes"]
    return Window(float(percent), _int(value["resets_at"]), None if minutes is None else _int(minutes, 1))


def _record(value: Any) -> StokeRecord | None:
    if value is None:
        return None
    if value["trigger"] not in {"auto", "manual", "startup"}:
        raise ValueError("invalid trigger")
    error = value["error"]
    if error is not None and not isinstance(error, str):
        raise ValueError("invalid error")
    opened = value["opened_new_window"]
    duration = value["duration_ms"]
    return StokeRecord(
        _int(value["at"]), value["trigger"], _bool(value["ok"]),
        None if error is None else short_error(error),
        None if opened is None else _bool(opened),
        None if duration is None else _int(duration),
    )


def load(path: Path, settings: Settings, tools: Mapping[str, Provider]) -> dict[str, ToolState]:
    """Load enabled tools, falling back to defaults for missing or corrupt data."""
    defaults = {
        name: ToolState(name, provider.label, settings.auto_default, False, None, None, None, None, 0, None)
        for name, provider in tools.items()
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state must be an object")
        states: dict[str, ToolState] = {}
        for name, default in defaults.items():
            if name not in data:
                states[name] = default
                continue
            item = data[name]
            warning = item["warning"]
            if warning not in {None, "did_not_open_window", "limits_unreadable", "stoke_failing"}:
                raise ValueError("invalid warning")
            next_at = item["next_stoke_at"]
            limits_at = item.get("limits_at")
            states[name] = ToolState(
                name, default.label, _bool(item["auto_stoke"]), False,
                _window(item["five_hour"]), _window(item["weekly"]), _record(item["last_stoke"]),
                None if next_at is None else _int(next_at), _int(item["consecutive_failures"]), warning,
                None if limits_at is None else _int(limits_at),
            )
        return states
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        logger.warning("State file missing or corrupt; using defaults")
        return defaults


def save(path: Path, states: Mapping[str, ToolState]) -> None:
    """Replace the state file atomically; never persist the live running flag."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump({name: serialize_tool(state) for name, state in states.items()}, file, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
