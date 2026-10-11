"""Shared data contract and its single JSON serializer."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Window:
    """One limit window reported by a CLI."""

    used_percent: float
    resets_at: int
    window_minutes: int | None


@dataclass
class StokeResult:
    """Limits and elapsed time from a successful stoke."""

    five_hour: Window
    weekly: Window | None
    duration_ms: int


@dataclass
class StokeRecord:
    """The outcome of one stoke attempt."""

    at: int
    trigger: str
    ok: bool
    error: str | None
    opened_new_window: bool | None
    duration_ms: int | None


@dataclass
class ToolState:
    """Current limits, scheduling and attempt state for one tool."""

    name: str
    label: str
    auto_stoke: bool
    running: bool
    five_hour: Window | None
    weekly: Window | None
    last_stoke: StokeRecord | None
    next_stoke_at: int | None
    consecutive_failures: int
    warning: str | None
    limits_at: int | None = None
    stale_resets: int = 0


def serialize_tool(state: ToolState, now: int | None = None) -> dict[str, Any]:
    """Serialize persisted fields, adding live fields only for API responses."""
    data = asdict(state)
    if now is None:
        del data["running"]
    else:
        data["window_open"] = state.five_hour is not None and state.five_hour.resets_at > now
    return data
