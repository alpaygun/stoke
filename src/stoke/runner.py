"""Execute fixed CLI arguments without a shell."""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence


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
