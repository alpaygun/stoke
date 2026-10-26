"""Append-only cycle observations with bounded retention and atomic pruning."""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
CYCLE_RETENTION_SECONDS = 30 * 86400


@dataclass(frozen=True)
class CycleRecord:
    """Raw timing and the already-sanitized outcome of one completed attempt."""

    tool: str
    trigger: str
    ok: bool
    error: str | None
    attempt_at: int
    completed_at: int
    duration_ms: int
    delay_seconds: int
    previous_end: int | None
    new_end: int | None
    opened: bool | None


def _record(value: Any) -> CycleRecord:
    if not isinstance(value, dict) or set(value) != set(CycleRecord.__dataclass_fields__):
        raise ValueError("invalid cycle record")
    for key in ("attempt_at", "completed_at", "duration_ms", "delay_seconds"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("invalid cycle integer")
    for key in ("previous_end", "new_end"):
        if value[key] is not None and (type(value[key]) is not int or value[key] < 0):
            raise ValueError("invalid cycle window")
    if (not isinstance(value["tool"], str) or not isinstance(value["trigger"], str)
            or type(value["ok"]) is not bool
            or (value["opened"] is not None and type(value["opened"]) is not bool)
            or (value["error"] is not None and not isinstance(value["error"], str))):
        raise ValueError("invalid cycle outcome")
    return CycleRecord(**value)


class CycleLog:
    """Synchronous file operations, serialized by the service's event loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._warned_corrupt = False

    def _read(self) -> tuple[list[CycleRecord], bool]:
        records = []
        corrupt = False
        try:
            file = self.path.open("rb")
        except FileNotFoundError:
            return records, corrupt
        with file:
            for line in file:
                try:
                    records.append(_record(json.loads(line)))
                except (ValueError, UnicodeError, RecursionError):
                    corrupt = True
        if corrupt and not self._warned_corrupt:
            logger.warning("Skipping corrupt cycle log lines")
            self._warned_corrupt = True
        return records, corrupt

    def read(self, now: int, *, tool: str | None = None, days: int = 30) -> list[dict[str, Any]]:
        """Return retained observations, newest first, without modifying storage."""
        records, _ = self._read()
        cutoff = now - days * 86400
        return [asdict(record) for record in sorted(records, key=lambda record: record.attempt_at, reverse=True)
                if record.attempt_at >= cutoff and (tool is None or record.tool == tool)]

    def _replace(self, records: list[CycleRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as file:
                temporary = Path(file.name)
                for record in records:
                    file.write(json.dumps(asdict(record), allow_nan=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def prune(self, now: int) -> None:
        """Drop expired and corrupt lines, replacing the file only when needed."""
        records, corrupt = self._read()
        retained = [record for record in records if record.attempt_at >= now - CYCLE_RETENTION_SECONDS]
        if corrupt or len(retained) != len(records):
            self._replace(retained)

    def append(self, record: CycleRecord, now: int) -> None:
        """Prune expired observations before appending one complete JSON line."""
        self.prune(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(record), allow_nan=False) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())
