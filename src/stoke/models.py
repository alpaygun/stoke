"""Shared data contract and its single JSON serializer."""

import unicodedata
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Window:
    """One limit window reported by a CLI."""

    used_percent: float
    resets_at: int
    window_minutes: int | None


def sanitize_reset_title(title: str) -> str:
    """Keep vendor titles compact and free of control characters."""
    clean = "".join(character for character in title if unicodedata.category(character) not in {"Cc", "Cf"})
    return " ".join(clean.split())[:80].rstrip() or "Limit reset"


@dataclass
class ResetCredit:
    """One available limit reset, without vendor identifiers."""

    title: str
    granted_at: int | None
    expires_at: int


@dataclass
class ResetCredits:
    """Available resets ordered by expiry and their last read time."""

    available: int
    credits: list[ResetCredit]
    read_at: int


@dataclass
class LimitsSnapshot:
    """Limits from a read that sends no model message."""

    five_hour: Window
    weekly: Window | None
    resets: ResetCredits | None


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
    resets: ResetCredits | None = None
    refreshing: bool = False


def serialize_tool(state: ToolState, now: int | None = None, refreshable: bool = False) -> dict[str, Any]:
    """Serialize persisted fields, adding live fields only for API responses."""
    data = asdict(state)
    if now is None:
        del data["running"]
        del data["refreshing"]
    else:
        data["window_open"] = state.five_hour is not None and state.five_hour.resets_at > now
        data["refreshable"] = refreshable
    return data
