"""Validated JSON state loading and atomic persistence."""

import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .config import Settings
from .models import AutoRefresh, MANUAL_RESET_TITLES, ManualReset, Plan, PlanOutcome, ReadRecord, ResetCredit, ResetCredits, StokeRecord, ToolState, WeeklyReset, Window, sanitize_reset_title, serialize_tool, validate_plan
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


def _percent(value: Any) -> float:
    if type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("invalid percentage")
    return float(value)


def _window(value: Any) -> Window | None:
    if value is None:
        return None
    percent = _percent(value["used_percent"])
    minutes = value["window_minutes"]
    return Window(float(percent), _int(value["resets_at"]), None if minutes is None else _int(minutes, 1))


def _record(value: Any) -> StokeRecord | None:
    if value is None:
        return None
    if value["trigger"] not in {"auto", "manual", "startup", "planned"}:
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


def _reset_credit(entry: Any) -> ResetCredit:
    if not isinstance(entry, dict) or not isinstance(entry.get("title"), str) or "granted_at" not in entry:
        raise ValueError("invalid reset credit")
    granted = entry["granted_at"]
    return ResetCredit(
        sanitize_reset_title(entry["title"]), None if granted is None else _int(granted),
        _int(entry.get("expires_at")),
    )


def _resets(value: Any) -> ResetCredits | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("credits"), list):
        raise ValueError("invalid reset credits")
    credits = [_reset_credit(entry) for entry in value["credits"]]
    credits.sort(key=lambda credit: credit.expires_at)
    return ResetCredits(_int(value.get("available")), credits, _int(value.get("read_at")))


def _plan(value: Any) -> Plan | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid plan")
    validate_plan(value["time"], value["repeat"], value["auto_after"])
    retry = value["retry_at"]
    return Plan(
        value["time"], value["repeat"], value["auto_after"], _int(value["target_at"]),
        None if retry is None else _int(retry),
    )


def _plan_outcome(value: Any) -> PlanOutcome | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value["result"] not in {"done", "missed", "broken"}:
        raise ValueError("invalid plan outcome")
    return PlanOutcome(_int(value["target_at"]), value["result"], _int(value["at"]))


def _auto_refresh(value: Any) -> AutoRefresh:
    """Normalize legacy intervals, rounding half minutes up without losing state."""
    if not isinstance(value, dict):
        raise ValueError("invalid auto refresh")
    enabled, seconds = value.get("enabled", False), value.get("seconds", 300)
    _bool(enabled)
    if type(seconds) is not int or not 60 <= seconds <= 86400:
        raise ValueError("invalid stored auto refresh interval")
    return AutoRefresh(enabled, min(3600, max(60, ((seconds + 30) // 60) * 60)))


def _read_record(value: Any) -> ReadRecord | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid read record")
    error = value.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("invalid error")
    return ReadRecord(_int(value.get("at")), _bool(value.get("ok")),
                      None if error is None else short_error(error), _bool(value.get("signed_out")))


def _manual_resets(value: Any) -> list[ManualReset]:
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("invalid manual resets")
    entries = []
    for entry in value:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or re.fullmatch(r"[0-9a-f]{12}", entry["id"]) is None
                or not isinstance(entry.get("title"), str) or entry["title"] not in MANUAL_RESET_TITLES):
            raise ValueError("invalid manual reset")
        entries.append(ManualReset(entry["id"], entry["title"], _int(entry.get("expires_at")), _int(entry.get("added_at"))))
    return sorted(entries, key=lambda entry: entry.expires_at)


def _weekly_resets(value: Any) -> list[WeeklyReset]:
    if not isinstance(value, list):
        raise ValueError("invalid weekly resets")
    entries = []
    for entry in value:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or re.fullmatch(r"[0-9a-f]{12}", entry["id"]) is None
                or not isinstance(entry.get("kind"), str) or entry["kind"] not in {"early", "credit"}):
            raise ValueError("invalid weekly reset")
        credits = entry.get("credits_before")
        if credits is not None and not isinstance(credits, list):
            raise ValueError("invalid weekly reset credits")
        entries.append(WeeklyReset(
            entry["id"], entry["kind"], _int(entry.get("from_at")), _int(entry.get("to_at")),
            _percent(entry.get("before_used")), _int(entry.get("before_resets_at")),
            _percent(entry.get("after_used")), _int(entry.get("after_resets_at")),
            _bool(entry.get("dismissed", False)),
            None if credits is None else [_reset_credit(credit) for credit in credits],
        ))
    return entries


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
            if warning not in {None, "auto_stopped", "did_not_open_window", "limits_unreadable", "stoke_failing"}:
                raise ValueError("invalid warning")
            next_at = item["next_stoke_at"]
            limits_at = item.get("limits_at")
            states[name] = ToolState(
                name, default.label, _bool(item["auto_stoke"]), False,
                _window(item["five_hour"]), _window(item["weekly"]), _record(item["last_stoke"]),
                None if next_at is None else _int(next_at), _int(item["consecutive_failures"]), warning,
                None if limits_at is None else _int(limits_at),
                _int(item.get("stale_resets", 0)),
                _resets(item.get("resets")),
                plan=_plan(item.get("plan")),
                plan_outcome=_plan_outcome(item.get("plan_outcome")),
                auto_refresh=_auto_refresh(item.get("auto_refresh", {})),
                last_read=_read_record(item.get("last_read")),
                manual_resets=_manual_resets(item.get("manual_resets", [])),
                weekly_resets=_weekly_resets(item.get("weekly_resets", [])),
            )
        return states
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        logger.warning("State file missing or corrupt; using defaults")
        return defaults


def save(path: Path, states: Mapping[str, ToolState]) -> None:
    """Replace the state file atomically; never persist live operation flags."""
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
