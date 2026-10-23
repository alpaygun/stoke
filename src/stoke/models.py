"""Shared data contract and its single JSON serializer."""

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import Settings

PlanRepeat = Literal["once", "daily"]
PlanResult = Literal["done", "missed", "broken"]


def validate_plan(time: str, repeat: PlanRepeat, auto_after: bool | None) -> None:
    """Require a local HH:MM time and strict plan options."""
    if not isinstance(time, str) or re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time, re.ASCII) is None:
        raise ValueError("time must be HH:MM")
    if not isinstance(repeat, str) or repeat not in {"once", "daily"}:
        raise ValueError("repeat must be once or daily")
    if auto_after is not None and type(auto_after) is not bool:
        raise ValueError("auto_after must be bool or null")


@dataclass
class Plan:
    """One pending occurrence of a local-time stoke plan."""

    time: str
    repeat: PlanRepeat
    auto_after: bool | None
    target_at: int
    retry_at: int | None


@dataclass
class PlanOutcome:
    """The most recently closed plan occurrence."""

    target_at: int
    result: PlanResult
    at: int


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
    observed_at: int | None = None


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
class AutoRefresh:
    """Persisted opt-in usage reading and its interval."""

    enabled: bool = False
    seconds: int = 300


def validate_auto_refresh(enabled: bool, seconds: int) -> None:
    """Reject coercion and intervals outside one minute to one day."""
    if type(enabled) is not bool:
        raise ValueError("enabled must be bool")
    if type(seconds) is not int or not 60 <= seconds <= 86400:
        raise ValueError("seconds must be an integer between 60 and 86400")


@dataclass
class ReadRecord:
    """Outcome of the most recent usage read, independent of stokes."""

    at: int
    ok: bool
    error: str | None
    signed_out: bool


MANUAL_RESET_TITLES = {"Full reset", "5-hour reset"}


@dataclass
class ManualReset:
    """A reset credit entered by the user and never spent by stoke."""

    id: str
    title: str
    expires_at: int
    added_at: int


@dataclass
class WeeklyReset:
    """A permanent observation of an early weekly limit reset."""

    id: str
    kind: str
    from_at: int
    to_at: int
    before_used: float
    before_resets_at: int
    after_used: float
    after_resets_at: int
    dismissed: bool = False
    credits_before: list[ResetCredit] | None = None


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
    plan: Plan | None = None
    plan_outcome: PlanOutcome | None = None
    auto_refresh: AutoRefresh = field(default_factory=AutoRefresh)
    last_read: ReadRecord | None = None
    manual_resets: list[ManualReset] = field(default_factory=list)
    weekly_resets: list[WeeklyReset] = field(default_factory=list)


def serialize_tool(
    state: ToolState, now: int | None = None, refreshable: bool = False,
    *, settings: "Settings | None" = None, manual_resets_allowed: bool = False,
    auth_labels: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Serialize persisted fields, adding live fields only for API responses."""
    data = asdict(state)
    if now is None:
        del data["running"]
        del data["refreshing"]
    else:
        # Import after models are defined; the scheduler owns the scheduling rules.
        from .scheduler import PLAN_QUIET_SECONDS, effective_next

        if settings is None:
            raise ValueError("settings required for live serialization")
        data["window_open"] = state.five_hour is not None and state.five_hour.resets_at > now
        data["refreshable"] = refreshable
        data["manual_resets_allowed"] = manual_resets_allowed
        data["manual_resets"] = [asdict(entry) for entry in state.manual_resets if entry.expires_at > now]
        for entry in data["weekly_resets"]:
            del entry["credits_before"]
        data["auth"] = {key: dict(value) for key, value in (auth_labels or {}).items()}
        expires = settings.claude_token_expires if state.name == "claude" else None
        if state.name == "claude" and "stoke" in data["auth"]:
            data["auth"]["stoke"]["expires_at"] = expires
        if state.warning is None:
            if state.last_read is not None and state.last_read.signed_out:
                data["warning"] = "read_signed_out"
            elif expires is not None and expires - now < 30 * 86400:
                data["warning"] = "token_expiring"
        data["upcoming_at"], data["upcoming_planned"] = effective_next(state, settings)
        data["quiet_from"] = state.plan.target_at - PLAN_QUIET_SECONDS if state.plan is not None else None
    return data
