"""Selector loop wakeups when AF_UNIX socket sends are blocked."""

import asyncio
import errno
import os
import signal
import socket
import stat
import sys
import threading
from asyncio.selector_events import BaseSelectorEventLoop
from concurrent.futures import Future
from contextlib import closing

import pytest

from conftest import (
    SELF_PIPE_PATCHED,
    _make_self_pipe,
    _read_from_self,
    _write_to_self,
    self_pipe_send_blocked,
)


class PipeSelectorEventLoop(asyncio.SelectorEventLoop):
    """Exercise the replacements independently of the global patch."""

    _make_self_pipe = _make_self_pipe
    _read_from_self = _read_from_self
    _write_to_self = _write_to_self


def require_linux_pipe2() -> None:
    if not sys.platform.startswith("linux") or not hasattr(os, "pipe2"):
        pytest.skip("requires Linux with pipe2")


@pytest.fixture
def pipe_loop_class(monkeypatch: pytest.MonkeyPatch) -> type[PipeSelectorEventLoop]:
    """Simulate a sandbox that denies socket sends throughout the test."""
    require_linux_pipe2()

    def blocked_send(self: socket.socket, data: bytes) -> int:
        raise PermissionError(errno.EPERM, "socket send denied")

    monkeypatch.setattr(socket.socket, "send", blocked_send)
    return PipeSelectorEventLoop


def test_self_pipe_patch_matches_probe() -> None:
    """Install the replacement only when the environment needs it."""
    blocked = self_pipe_send_blocked()
    assert SELF_PIPE_PATCHED is blocked
    assert (BaseSelectorEventLoop._make_self_pipe is _make_self_pipe) is blocked


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PermissionError(errno.EPERM, "socket send denied"), True),
        (None, False),
        (OSError(errno.EBADF, "bad file descriptor"), False),
    ],
)
def test_self_pipe_probe_send(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None, expected: bool,
) -> None:
    """Only a permission error from send activates the patch."""
    require_linux_pipe2()

    def send(self: socket.socket, data: bytes) -> int:
        if error is not None:
            raise error
        return len(data)

    monkeypatch.setattr(socket.socket, "send", send)
    assert self_pipe_send_blocked() is expected


def test_self_pipe_probe_socketpair_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permission error creating the socketpair leaves asyncio alone."""
    require_linux_pipe2()

    def socketpair() -> tuple[socket.socket, socket.socket]:
        raise PermissionError(errno.EPERM, "socketpair denied")

    monkeypatch.setattr(socket, "socketpair", socketpair)
    assert self_pipe_send_blocked() is False


def test_self_pipe_descriptors_close(pipe_loop_class: type[PipeSelectorEventLoop]) -> None:
    """The standard selector-loop close path releases both pipe descriptors."""
    with closing(pipe_loop_class()) as loop:
        read_fd = loop._ssock.fileno()
        write_fd = loop._csock.fileno()
        for fd in (read_fd, write_fd):
            assert stat.S_ISFIFO(os.fstat(fd).st_mode)

    assert loop._ssock is None
    assert loop._csock is None
    for fd in (read_fd, write_fd):
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF


def test_self_pipe_wakes_selector_from_thread(
    pipe_loop_class: type[PipeSelectorEventLoop], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback posted from another thread wakes a waiting selector."""
    with closing(pipe_loop_class()) as loop:
        selector_waiting = threading.Event()
        select = loop._selector.select

        def select_with_notification(timeout: float | None = None) -> list:
            if timeout is None or timeout > 0:
                selector_waiting.set()
            return select(timeout)

        monkeypatch.setattr(loop._selector, "select", select_with_notification)

        async def scenario() -> None:
            result = loop.create_future()

            def post_result() -> None:
                if selector_waiting.wait(timeout=5):
                    loop.call_soon_threadsafe(result.set_result, 42)

            worker = threading.Thread(target=post_result, daemon=True)
            worker.start()
            try:
                assert await asyncio.wait_for(result, timeout=5) == 42
            finally:
                worker.join(timeout=5)
                assert not worker.is_alive()

        loop.run_until_complete(scenario())


def test_self_pipe_to_thread_and_executor_shutdown(
    pipe_loop_class: type[PipeSelectorEventLoop],
) -> None:
    """Thread work returns its result and asyncio.run finishes executor shutdown."""
    outcome: Future[int] = Future()

    async def scenario() -> int:
        return await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=5)

    def run() -> None:
        try:
            outcome.set_result(asyncio.run(scenario(), loop_factory=pipe_loop_class))
        except BaseException as exc:
            outcome.set_exception(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=6)
    assert not worker.is_alive(), "asyncio.run did not finish"
    assert outcome.result(timeout=0) == 42


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="requires SIGUSR1")
def test_self_pipe_delivers_signal(pipe_loop_class: type[PipeSelectorEventLoop]) -> None:
    """The pipe carries a signal byte to the event loop's signal handler."""
    with closing(pipe_loop_class()) as loop:
        received = asyncio.Event()
        signum = signal.SIGUSR1
        previous_handler = signal.getsignal(signum)
        previous_wakeup_fd = signal.set_wakeup_fd(-1)
        try:
            loop.add_signal_handler(signum, received.set)
            signal.raise_signal(signum)
            loop.run_until_complete(asyncio.wait_for(received.wait(), timeout=5))
        finally:
            try:
                loop.remove_signal_handler(signum)
            finally:
                signal.signal(signum, previous_handler)
                signal.set_wakeup_fd(previous_wakeup_fd)
