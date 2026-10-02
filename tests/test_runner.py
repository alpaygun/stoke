"""Subprocess orchestration tested exclusively with fake process objects."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from stoke.runner import run_command


class FakeProcess:
    """A captured process whose communication can block until killed."""

    def __init__(self, blocked: bool = False) -> None:
        self.returncode: int | None = None if blocked else 0
        self.killed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def communicate(self) -> tuple[bytes, bytes]:
        """Return deterministic output once released."""
        self.started.set()
        await self.release.wait()
        return b"stdout\xff", b"stderr"

    def kill(self) -> None:
        """Unblock communication as a terminated process would."""
        self.killed = True
        self.returncode = -9
        self.release.set()


def test_runner_uses_exec_captures_and_passes_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    process = FakeProcess()
    async def create(*argv: str, **kwargs: Any) -> FakeProcess:
        calls.append((argv, kwargs))
        return process
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    result = asyncio.run(run_command(["fixed-binary", "fixed-argument"], tmp_path, 10, {"CUSTOM": "value"}))
    assert result.returncode == 0 and result.stdout == "stdout\ufffd" and result.stderr == "stderr"
    assert result.duration_ms >= 0 and not result.timed_out
    assert calls[0] == (("fixed-binary", "fixed-argument"), {
        "cwd": tmp_path, "env": {"CUSTOM": "value"},
        "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE,
    })
    assert not process.killed


def test_runner_kills_and_reaps_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess(blocked=True)
    async def create(*argv: str, **kwargs: Any) -> FakeProcess:
        assert kwargs["env"] is None
        return process
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    result = asyncio.run(run_command(["fixed"], tmp_path, 0))
    assert result.timed_out and result.returncode == -9 and process.killed
    assert result.stdout == "stdout\ufffd"


def test_runner_kills_and_reaps_on_cancellation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        process = FakeProcess(blocked=True)
        async def create(*argv: str, **kwargs: Any) -> FakeProcess:
            return process
        monkeypatch.setattr("asyncio.create_subprocess_exec", create)
        task = asyncio.create_task(run_command(["fixed"], tmp_path, 10))
        await process.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.returncode == -9
    asyncio.run(scenario())
