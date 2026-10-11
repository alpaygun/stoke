"""Pure scheduling rules and an event-driven loop for each independent tool."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, cast

from . import clock, store
from .config import Settings
from .models import ResetCredits, StokeRecord, StokeResult, ToolState, Window
from .providers.base import LimitReader, LimitsUnreadable, Provider, ProviderError, short_error

logger = logging.getLogger(__name__)
Trigger = Literal["auto", "manual", "startup"]
STALE_RESET_LIMIT = 4
REFRESH_BUSY_RETRY_SECONDS = 10


class AlreadyRunning(Exception):
    """A conflicting operation is already in flight for a tool."""


class RefreshUnsupported(Exception):
    """A tool has no official no-stoke limit reader."""


def next_after_success(result: StokeResult, settings: Settings) -> int:
    """Wait for the reported reset plus the configured delay."""
    return result.five_hour.resets_at + settings.delay_seconds


def next_after_failure(now: int, consecutive_failures: int, settings: Settings) -> int:
    """Apply the failure backoff, repeating its last value indefinitely."""
    index = min(max(consecutive_failures - 1, 0), len(settings.retry_seconds) - 1)
    return now + settings.retry_seconds[index]


def startup_decision(state: ToolState, now: int, settings: Settings) -> Literal["stoke_now"] | int:
    """Reuse a future window; otherwise stoke immediately."""
    if state.five_hour is not None and state.five_hour.resets_at > now:
        return state.five_hour.resets_at + settings.delay_seconds
    return "stoke_now"


def opened_new_window(
    previous_five_hour: Window | None, attempt_at: int, new_five_hour: Window,
) -> bool | None:
    """Assess a new window only within 15 minutes after a known reset."""
    if previous_five_hour is None:
        return None
    if not previous_five_hour.resets_at <= attempt_at <= previous_five_hour.resets_at + 900:
        return None
    return new_five_hour.resets_at >= attempt_at + 4 * 3600


def compute_warning(state: ToolState) -> str | None:
    """Return the highest-priority problem from the latest attempt."""
    if state.stale_resets >= STALE_RESET_LIMIT and not state.auto_stoke:
        return "auto_stopped"
    record = state.last_stoke
    if record is not None and record.opened_new_window is False:
        return "did_not_open_window"
    if record is not None and record.error == "limits unreadable":
        return "limits_unreadable"
    if state.consecutive_failures >= 3:
        return "stoke_failing"
    return None


class Scheduler:
    """Own tool state, per-tool locks/events, and lifecycle-managed tasks."""

    def __init__(
        self, providers: Mapping[str, Provider], states: dict[str, ToolState],
        settings: Settings, path: Path | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.readers = {
            name: cast(LimitReader, provider) for name, provider in providers.items()
            if callable(getattr(provider, "read_limits", None))
        }
        self.states = states
        self.settings = settings
        self.path = path if path is not None else settings.data_dir / "state.json"
        self.locks = {name: asyncio.Lock() for name in providers}
        self.events = {name: asyncio.Event() for name in providers}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._attempts: set[asyncio.Task[object]] = set()
        self._startup_pending = {name: False for name in providers}

    def _persist(self) -> None:
        store.save(self.path, self.states)

    def _schedule_startup(self, name: str) -> None:
        state = self.states[name]
        now = clock.now()
        decision = startup_decision(state, now, self.settings)
        self._startup_pending[name] = decision == "stoke_now"
        state.next_stoke_at = now if decision == "stoke_now" else decision

    async def start(self) -> None:
        """Initialize schedules and launch exactly one loop per enabled tool."""
        if self.tasks:
            return
        for name, state in self.states.items():
            state.running = False
            state.refreshing = False
            state.warning = compute_warning(state)
            if state.auto_stoke:
                self._schedule_startup(name)
            else:
                state.next_stoke_at = None
        self._persist()
        self.tasks = {name: asyncio.create_task(self._loop(name), name=f"stoke:{name}") for name in self.providers}
        self.refresh_tasks = {
            name: asyncio.create_task(self._refresh_loop(name), name=f"refresh:{name}") for name in self.readers
        }

    async def stop(self) -> None:
        """Cancel loops and manual attempts, and wait for their cleanup."""
        tasks = set(self.tasks.values()) | set(self.refresh_tasks.values()) | self._attempts
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.refresh_tasks.clear()

    def can_refresh(self, name: str) -> bool:
        """Report the provider's optional read capability."""
        return name in self.readers

    async def refresh_limits(self, name: str) -> ToolState:
        """Update usage only; a read must never determine stoke scheduling."""
        if name not in self.readers:
            raise RefreshUnsupported(name)
        state = self.states[name]
        if state.refreshing or state.running:
            raise AlreadyRunning(name)
        state.refreshing = True
        task = asyncio.current_task()
        if task is not None:
            self._attempts.add(task)
        try:
            try:
                snapshot = await self.readers[name].read_limits()
            except ProviderError as error:
                logger.warning("tool=%s refresh failed: %s", name, short_error(str(error)))
                raise
            except Exception as error:
                logger.warning("tool=%s refresh failed: %s", name, type(error).__name__)
                raise ProviderError("unexpected provider error") from None
            now = clock.now()
            if state.five_hour is not None:
                state.five_hour = Window(
                    snapshot.five_hour.used_percent, state.five_hour.resets_at, state.five_hour.window_minutes,
                )
            if snapshot.weekly is not None:
                state.weekly = snapshot.weekly
            state.limits_at = now
            if snapshot.resets is not None:
                state.resets = ResetCredits(snapshot.resets.available, snapshot.resets.credits, now)
            self._persist()
            return state
        finally:
            state.refreshing = False
            if task is not None:
                self._attempts.discard(task)

    async def _refresh_loop(self, name: str) -> None:
        """Read at startup and each interval, surviving failures and overlap."""
        while True:
            delay = self.settings.refresh_seconds
            try:
                await self.refresh_limits(name)
            except AlreadyRunning:
                delay = REFRESH_BUSY_RETRY_SECONDS
            except ProviderError:
                pass
            except Exception as error:
                logger.warning("tool=%s refresh failed: %s", name, type(error).__name__)
            await asyncio.sleep(delay)

    def set_auto(self, name: str, enabled: bool) -> ToolState:
        """Persist the switch and immediately wake the tool's waiting loop."""
        state = self.states[name]
        changed = state.auto_stoke != enabled
        state.stale_resets = 0
        state.auto_stoke = enabled
        state.warning = compute_warning(state)
        if not enabled:
            state.next_stoke_at = None
            self._startup_pending[name] = False
        elif changed:
            self._schedule_startup(name)
        self._persist()
        self.events[name].set()
        return state

    async def stoke_now(self, name: str, trigger: Trigger) -> ToolState:
        """Complete one attempt, rejecting overlap without waiting for the lock."""
        lock = self.locks[name]
        if lock.locked():
            raise AlreadyRunning(name)
        async with lock:
            task = asyncio.current_task()
            if task is not None:
                self._attempts.add(task)
            state = self.states[name]
            attempt_at = clock.now()
            started = time.perf_counter()
            previous = state.five_hour
            auto_stopped = state.stale_resets >= STALE_RESET_LIMIT and not state.auto_stoke
            state.running = True
            self._startup_pending[name] = False
            try:
                self._persist()
                try:
                    result = await self.providers[name].stoke()
                except LimitsUnreadable:
                    self._record_failure(state, attempt_at, trigger, "limits unreadable", started)
                except ProviderError as error:
                    self._record_failure(state, attempt_at, trigger, str(error), started)
                except Exception:
                    # Unexpected provider exceptions must not expose their potentially secret text.
                    self._record_failure(state, attempt_at, trigger, "unexpected provider error", started)
                else:
                    completed_at = clock.now()
                    if result.five_hour.resets_at <= completed_at:
                        self._record_failure(
                            state, attempt_at, trigger, "reset time not in the future", started, stale=True,
                        )
                    else:
                        state.five_hour = result.five_hour
                        state.weekly = result.weekly
                        state.limits_at = completed_at
                        state.consecutive_failures = 0
                        state.stale_resets = 0
                        if auto_stopped:
                            state.auto_stoke = True
                        state.last_stoke = StokeRecord(
                            attempt_at, trigger, True, None,
                            opened_new_window(previous, attempt_at, result.five_hour), result.duration_ms,
                        )
                        state.next_stoke_at = next_after_success(result, self.settings) if state.auto_stoke else None
                self._startup_pending[name] = False
                state.warning = compute_warning(state)
                self._log_stoke(state)
            finally:
                state.running = False
                if task is not None:
                    self._attempts.discard(task)
                self._persist()
                self.events[name].set()
        return state

    def _record_failure(
        self, state: ToolState, at: int, trigger: Trigger, error: str, started: float,
        *, stale: bool = False,
    ) -> None:
        state.stale_resets = (
            state.stale_resets + 1
            if stale and (state.auto_stoke or state.stale_resets >= STALE_RESET_LIMIT) else 0
        )
        state.consecutive_failures += 1
        state.last_stoke = StokeRecord(at, trigger, False, short_error(error), None, int((time.perf_counter() - started) * 1000))
        state.next_stoke_at = (
            next_after_failure(clock.now(), state.consecutive_failures, self.settings)
            if state.auto_stoke else None
        )
        if state.stale_resets >= STALE_RESET_LIMIT and state.auto_stoke:
            state.auto_stoke = False
            state.next_stoke_at = None
            logger.error("tool=%s auto-stoke stopped: repeated stale reset times", state.name)

    def _log_stoke(self, state: ToolState) -> None:
        record = state.last_stoke
        if record is None:
            return
        if record.ok and state.five_hour is not None:
            try:
                detail = "resets_at=" + datetime.fromtimestamp(state.five_hour.resets_at, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                detail = "resets_at=out-of-range"
        else:
            detail = "error=" + short_error(record.error or "stoke failed")
        logger.info("tool=%s trigger=%s ok=%s duration_ms=%s %s", state.name, record.trigger, record.ok, record.duration_ms, detail)

    async def _loop(self, name: str) -> None:
        event = self.events[name]
        state = self.states[name]
        while True:
            try:
                event.clear()
                if not state.auto_stoke or state.next_stoke_at is None or state.running:
                    await event.wait()
                    continue
                delay = state.next_stoke_at - clock.now()
                if delay > 0:
                    try:
                        await asyncio.wait_for(event.wait(), delay)
                    except TimeoutError:
                        pass
                    continue
                trigger: Trigger = "startup" if self._startup_pending[name] else "auto"
                try:
                    await self.stoke_now(name, trigger)
                except AlreadyRunning:
                    await event.wait()
            except Exception as error:
                logger.error("tool=%s loop error: %s", name, type(error).__name__)
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), self.settings.retry_seconds[0])
                except TimeoutError:
                    pass
