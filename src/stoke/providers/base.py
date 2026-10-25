"""Provider contract, safe failures, and shared parsing primitives."""

import json
import math
import re
from typing import Any, Iterator, Protocol

from ..models import LimitsSnapshot, StokeResult, Window

_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+|eyJ[A-Za-z0-9_\-]{20,}")


def redact(message: str) -> str:
    """Mask Claude tokens and JWT-like strings before truncating or logging."""
    return _SECRET.sub("[REDACTED]", message)


def short_error(message: str) -> str:
    """Produce a single safe line of at most 200 characters."""
    return " ".join(redact(message).split())[:200]


class ProviderError(Exception):
    """A provider operation failed; its message is safe for logs and the API."""

    def __init__(self, message: str) -> None:
        super().__init__(short_error(message))


class LimitsUnreadable(ProviderError):
    """The CLI succeeded but its limit data could not be read."""


class ReaderSignedOut(ProviderError):
    """The separate browser login used for usage reads is no longer valid."""


class Provider(Protocol):
    """One tool's fixed stoke operation."""

    name: str
    label: str
    manual_resets: bool
    reset_closes_window: bool
    auth_labels: dict[str, dict[str, str]]

    async def stoke(self) -> StokeResult: ...


class LimitReader(Protocol):
    """Optional provider capability for no-stoke limit reads."""

    name: str
    label: str

    async def read_limits(self) -> LimitsSnapshot: ...


def json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Yield JSONL objects, skipping malformed lines and nonobjects."""
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            yield value


def parse_window(data: Any, *, claude: bool = False) -> Window:
    """Validate a CLI window, converting Claude utilization to a percentage."""
    if not isinstance(data, dict):
        raise LimitsUnreadable("limits unreadable")
    percent = data.get("utilization" if claude else "used_percent")
    resets = data.get("resetsAt" if claude else "resets_at")
    minutes = data.get("window_minutes")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        raise LimitsUnreadable("limits unreadable")
    try:
        percent = float(percent) * (100 if claude else 1)
    except OverflowError:
        raise LimitsUnreadable("limits unreadable") from None
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise LimitsUnreadable("limits unreadable")
    if type(resets) is not int or resets < 0:
        raise LimitsUnreadable("limits unreadable")
    if minutes is not None and (type(minutes) is not int or minutes <= 0):
        raise LimitsUnreadable("limits unreadable")
    return Window(percent, resets, minutes)
