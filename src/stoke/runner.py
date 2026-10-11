"""Execute fixed CLI arguments without a shell."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


@dataclass
class CommandResult:
    """Captured output and measured execution outcome."""

    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


class Runner(Protocol):
    """Injectable asynchronous command runner."""

    async def __call__(
        self, argv: Sequence[str], cwd: Path, timeout_s: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class RpcError(Exception):
    """A safe app-server transport failure, containing no process output."""


class RpcRunner(Protocol):
    """Injectable line-JSON-RPC exchange."""

    async def __call__(
        self, argv: Sequence[str], cwd: Path, timeout_s: float,
        env: Mapping[str, str] | None, initialize: dict[str, Any],
        notifications: Sequence[dict[str, Any]], request: dict[str, Any],
    ) -> dict[str, Any]: ...


async def _shutdown_rpc(process: asyncio.subprocess.Process) -> None:
    """Let the wrapper close its native child before escalating shutdown."""
    if process.stdin is not None:
        process.stdin.close()
    for signal in (process.terminate, process.kill):
        try:
            await asyncio.wait_for(process.wait(), 5)
            return
        except TimeoutError:
            if process.returncode is None:
                try:
                    signal()
                except ProcessLookupError:
                    pass
    await process.wait()


async def run_jsonrpc(
    argv: Sequence[str], cwd: Path, timeout_s: float,
    env: Mapping[str, str] | None, initialize: dict[str, Any],
    notifications: Sequence[dict[str, Any]], request: dict[str, Any],
) -> dict[str, Any]:
    """Exchange JSONL messages and always close, then reap, the app-server."""
    process: asyncio.subprocess.Process | None = None
    try:
        async with asyncio.timeout(timeout_s):
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, env=None if env is None else dict(env),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=1_048_576,
                )
            except OSError:
                raise RpcError("could not be started") from None
            assert process.stdin is not None and process.stdout is not None

            async def write(message: dict[str, Any]) -> None:
                assert process is not None and process.stdin is not None
                process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
                await process.stdin.drain()

            async def response(expected_id: Any) -> dict[str, Any]:
                assert process is not None and process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise RpcError("app-server exited before answering")
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if isinstance(value, dict) and value.get("id") == expected_id:
                        return value

            await write(initialize)
            await response(initialize["id"])
            for notification in notifications:
                await write(notification)
            await write(request)
            return await response(request["id"])
    except TimeoutError:
        raise RpcError("timed out") from None
    except (BrokenPipeError, ConnectionResetError):
        raise RpcError("app-server exited before answering") from None
    finally:
        if process is not None:
            cleanup = asyncio.create_task(_shutdown_rpc(process))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError


async def run_command(
    argv: Sequence[str], cwd: Path, timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Capture a command, killing and reaping it on timeout or cancellation."""
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=None if env is None else dict(env),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), timeout_s)
    except TimeoutError:
        timed_out = True
        if process.returncode is None:
            process.kill()
        stdout, stderr = await output
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await output
        raise
    return CommandResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        duration_ms=int((time.perf_counter() - started) * 1000),
        timed_out=timed_out,
    )
