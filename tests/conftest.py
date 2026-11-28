"""Shared test settings and providers; no test may execute a real command."""

import asyncio
import os
import socket
import sys
from asyncio.selector_events import BaseSelectorEventLoop
from pathlib import Path
from typing import Any, Callable


# Linux sandboxes that forbid send on AF_UNIX sockets stall asyncio's cross-thread
# wakeups. Use a pipe only when the runtime probe sees PermissionError from send.
# When send works, asyncio keeps its default self-pipe. This only fixes the selector
# loop's self-pipe. Other AF_UNIX users, such as multiprocessing and Unix-socket
# clients, can still fail under the same restriction.
def self_pipe_send_blocked() -> bool:
    """Check whether the Linux selector loop needs a pipe for wakeups."""
    if not sys.platform.startswith("linux") or not hasattr(os, "pipe2"):
        return False

    try:
        reader, writer = socket.socketpair()
        with reader, writer:
            try:
                writer.send(b"\0")
            except PermissionError:
                return True
    except OSError:
        pass
    return False


def _make_self_pipe(self: BaseSelectorEventLoop) -> None:
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    self._ssock = os.fdopen(read_fd, "rb", buffering=0)
    self._csock = os.fdopen(write_fd, "wb", buffering=0)
    self._internal_fds += 1
    self._add_reader(read_fd, self._read_from_self)


def _read_from_self(self: BaseSelectorEventLoop) -> None:
    while True:
        try:
            data = os.read(self._ssock.fileno(), 4096)
        except InterruptedError:
            continue
        except BlockingIOError:
            break
        if not data:
            break
        self._process_self_data(data)


def _write_to_self(self: BaseSelectorEventLoop) -> None:
    csock = self._csock
    if csock is None:
        return
    try:
        os.write(csock.fileno(), b"\0")
    except (OSError, ValueError):
        pass


SELF_PIPE_PATCHED = self_pipe_send_blocked()
if SELF_PIPE_PATCHED:
    BaseSelectorEventLoop._make_self_pipe = _make_self_pipe
    BaseSelectorEventLoop._read_from_self = _read_from_self
    BaseSelectorEventLoop._write_to_self = _write_to_self


import pytest

from stoke.config import Settings, load_settings
from stoke.providers.claude import ClaudeProvider
from stoke.providers.codex import CodexProvider
from stoke.models import LimitsSnapshot, ResetCredit, ResetCredits, StokeResult, Window


class FakeProvider:
    """Deterministic provider with event-controlled blocking for concurrency tests."""

    def __init__(self, name: str = "claude", result: StokeResult | Exception | None = None) -> None:
        self.name = name
        real = ClaudeProvider if name == "claude" else CodexProvider
        self.reset_closes_window = real.reset_closes_window
        self.auth_labels = real.auth_labels
        self.label = {"claude": "Claude", "codex": "Codex"}[name]
        self.result = result if result is not None else StokeResult(Window(2.0, 19000, 300), None, 12)
        self.calls = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def stoke(self) -> StokeResult:
        """Signal invocation, optionally block, then return or fail."""
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeReader(FakeProvider):
    """A provider whose independent read can be controlled without I/O."""

    def __init__(self, name: str = "codex") -> None:
        super().__init__(name)
        self.read_result: LimitsSnapshot | Exception = LimitsSnapshot(
            Window(8, 99999, 301), Window(29, 600000, 10080),
            ResetCredits(1, [ResetCredit("Full reset", 10, 700000)], 0),
        )
        self.read_calls = 0
        self.read_mode_calls: list[str] = []
        self.read_started = asyncio.Event()
        self.read_release: asyncio.Event | None = None

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        self.read_calls += 1
        self.read_mode_calls.append(mode)
        self.read_started.set()
        if self.read_release is not None:
            await self.read_release.wait()
        if isinstance(self.read_result, Exception):
            raise self.read_result
        return self.read_result


@pytest.fixture(autouse=True)
def forbid_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately if any test accidentally reaches a real subprocess."""
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Tests must inject a runner; real subprocesses are forbidden")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    """Build isolated settings without reading the host environment."""
    def create(**overrides: str) -> Settings:
        return load_settings({
            "STOKE_DATA_DIR": str(tmp_path / "data"),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "STOKE_AUTO_DEFAULT": "0",
            # Local-time expectations are written for this zone, independent of the default.
            "STOKE_TIMEZONE": "Europe/Istanbul",
            **overrides,
        })
    return create


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """Read an explicitly named CLI fixture."""
    def read(name: str) -> str:
        return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")
    return read
