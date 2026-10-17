"""Subprocess orchestration tested exclusively with fake process objects."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from stoke.runner import RpcError, run_command, run_jsonrpc


class FakeProcess:
    """A captured process whose communication can block until killed."""

    def __init__(self, blocked: bool = False) -> None:
        self.returncode: int | None = None if blocked else 0
        self.killed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.input: bytes | None = None
        if not blocked:
            self.release.set()

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        """Return deterministic output once released."""
        self.input = input
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


@pytest.mark.parametrize("stdin", [b"request\n", b""])
def test_runner_stdin_is_piped_and_passed_exactly(stdin: bytes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess()
    async def create(*argv: str, **kwargs: Any) -> FakeProcess:
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return process
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    asyncio.run(run_command(["fixed"], tmp_path, 10, stdin=stdin))
    assert process.input == stdin


@pytest.mark.parametrize("stdin", [None, b"control\n"])
def test_runner_kills_and_reaps_timeout(stdin: bytes | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = FakeProcess(blocked=True)
    async def create(*argv: str, **kwargs: Any) -> FakeProcess:
        assert kwargs["env"] is None
        return process
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    result = asyncio.run(run_command(["fixed"], tmp_path, 0, stdin=stdin))
    assert result.timed_out and result.returncode == -9 and process.killed
    assert result.stdout == "stdout\ufffd"
    assert process.input == stdin


@pytest.mark.parametrize("stdin", [None, b"control\n"])
def test_runner_kills_and_reaps_on_cancellation(stdin: bytes | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        process = FakeProcess(blocked=True)
        async def create(*argv: str, **kwargs: Any) -> FakeProcess:
            return process
        monkeypatch.setattr("asyncio.create_subprocess_exec", create)
        task = asyncio.create_task(run_command(["fixed"], tmp_path, 10, stdin=stdin))
        await process.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed and process.returncode == -9
        assert process.input == stdin
    asyncio.run(scenario())


class FakeRpcProcess:
    """Line transport with observable handshake and shutdown stages."""

    def __init__(self, lines: list[bytes], exit_on: str = "close", blocked: bool = False) -> None:
        self.stdin = self
        self.stdout = self
        self.lines = lines
        self.exit_on = exit_on
        self.stage = "started"
        self.blocked = blocked
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.read_started = asyncio.Event()
        self.release = asyncio.Event()

    def write(self, data: bytes) -> None:
        assert data.endswith(b"\n") and data.count(b"\n") == 1
        message = json.loads(data)
        self.messages.append(message)
        self.events.append(message["method"])

    async def drain(self) -> None:
        pass

    async def readline(self) -> bytes:
        self.read_started.set()
        if self.blocked:
            await self.release.wait()
        line = self.lines.pop(0) if self.lines else b""
        if line == b'{"id":0}\n':
            assert len(self.messages) == 1
            self.events.append("initialize answered")
        return line

    def close(self) -> None:
        self.events.append("close")
        self.stage = "close"

    def terminate(self) -> None:
        self.events.append("terminate")
        self.stage = "terminate"

    def kill(self) -> None:
        self.events.append("kill")
        self.stage = "kill"

    async def wait(self) -> int:
        self.events.append("wait")
        if self.stage != self.exit_on:
            raise TimeoutError
        self.returncode = 0
        return 0


INITIALIZE = {"method": "initialize", "id": 0}
NOTIFICATIONS = [{"method": "initialized"}]
REQUEST = {"method": "account/rateLimits/read", "id": 1}


def test_rpc_message_order_skips_noise_and_returns_whole_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    answer = {"id": 1, "result": {"usage": 3}}
    process = FakeRpcProcess([
        b"junk\n", b"\xff\n", b"[]\n", b"null\n", b'{"method":"notification"}\n',
        b'{"id":99}\n', b'{"id":1,"result":"too early"}\n', b'{"id":0}\n',
        b'{"id":2}\n', json.dumps(answer).encode() + b"\n",
    ])
    async def create(*argv: str, **kwargs: Any) -> FakeRpcProcess:
        assert argv == ("codex", "app-server")
        assert kwargs == {
            "cwd": tmp_path, "env": {"CUSTOM": "value"}, "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.DEVNULL, "limit": 1_048_576,
        }
        return process
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    assert asyncio.run(run_jsonrpc(("codex", "app-server"), tmp_path, 10, {"CUSTOM": "value"},
                                   INITIALIZE, NOTIFICATIONS, REQUEST)) == answer
    assert process.messages == [INITIALIZE, *NOTIFICATIONS, REQUEST]
    assert process.events == ["initialize", "initialize answered", "initialized", "account/rateLimits/read", "close", "wait"]


@pytest.mark.parametrize("exit_on,shutdown", [
    ("close", ["close", "wait"]),
    ("terminate", ["close", "wait", "terminate", "wait"]),
    ("kill", ["close", "wait", "terminate", "wait", "kill", "wait"]),
])
@pytest.mark.parametrize("outcome", ["success", "eof", "timeout", "cancel"])
def test_rpc_always_shuts_down_in_order(
    exit_on: str, shutdown: list[str], outcome: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def scenario() -> None:
        lines = [b'{"id":0}\n', b'{"id":1,"error":{"message":"failure"}}\n'] if outcome == "success" else []
        process = FakeRpcProcess(lines, exit_on, blocked=outcome in {"timeout", "cancel"})
        waits = []
        original_wait_for = asyncio.wait_for
        async def wait_for(awaitable: Any, timeout: float) -> Any:
            waits.append(timeout)
            return await original_wait_for(awaitable, timeout)
        monkeypatch.setattr("asyncio.wait_for", wait_for)
        async def create(*argv: str, **kwargs: Any) -> FakeRpcProcess:
            assert kwargs["env"] is None
            return process
        monkeypatch.setattr("asyncio.create_subprocess_exec", create)
        task = asyncio.create_task(run_jsonrpc(["fixed"], tmp_path, 0.01 if outcome == "timeout" else 10,
                                               None, INITIALIZE, NOTIFICATIONS, REQUEST))
        if outcome == "cancel":
            await process.read_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "success":
            assert (await task)["error"] == {"message": "failure"}
        else:
            message = "timed out" if outcome == "timeout" else "app-server exited before answering"
            with pytest.raises(RpcError, match=f"^{message}$"):
                await task
        assert process.events[process.events.index("close"):] == shutdown
        assert process.returncode == 0
        assert waits == [5] * (1 if exit_on == "close" else 2)
    asyncio.run(scenario())


def test_rpc_cancellation_during_cleanup_still_reaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        process = FakeRpcProcess([b'{"id":0}\n', b'{"id":1}\n'])
        cleanup_started, finish = asyncio.Event(), asyncio.Event()
        async def wait() -> int:
            cleanup_started.set()
            await finish.wait()
            process.returncode = 0
            return 0
        process.wait = wait  # type: ignore[method-assign]
        async def create(*argv: str, **kwargs: Any) -> FakeRpcProcess:
            return process
        monkeypatch.setattr("asyncio.create_subprocess_exec", create)
        task = asyncio.create_task(run_jsonrpc(["fixed"], tmp_path, 10, None, INITIALIZE, NOTIFICATIONS, REQUEST))
        await cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode == 0 and "close" in process.events
    asyncio.run(scenario())


def test_rpc_start_error_contains_no_process_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def create(*argv: str, **kwargs: Any) -> None:
        raise OSError("private process output")
    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    with pytest.raises(RpcError, match="^could not be started$"):
        asyncio.run(run_jsonrpc(["fixed"], tmp_path, 10, None, INITIALIZE, NOTIFICATIONS, REQUEST))
