"""Pure scheduling rules and an event-driven loop for each independent tool."""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, time as local_time, timedelta, timezone
from pathlib import Path
from typing import Literal, Mapping, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import clock, store
from .config import Settings
from .cycles import CycleLog, CycleRecord
from .models import (
    READ_MODES, THIRD_PARTY_MODES, AutoRefresh, GroupStokeResult, LimitsSnapshot, PendingVerify, Plan,
    PlanOutcome, PlanRepeat, PlanResult, QuotaGroup, ReadRecord, ResetCredit, ResetCredits, StokeRecord,
    StokeResult, ThirdPartyMode, ThirdPartyState, ThirdPartyWarningReason, ToolState, WeeklyReset, Window,
    group_window_confirmed, third_party_quota, validate_auto_refresh, validate_plan,
)
from .providers.base import (
    LimitReader, LimitsUnreadable, Provider, ProviderError, ReaderSignedOut, StokeNotDelivered,
    StokeSignedOut, StokeUnverified, ThirdPartyStoker, short_error,
)

logger = logging.getLogger(__name__)
Trigger = Literal["auto", "manual", "startup", "planned", "reset"]
STALE_RESET_LIMIT = 4
SIGNED_OUT_ERROR = "signed out"
REFRESH_BUSY_RETRY_SECONDS = 10
# Leave a margin after the CLI's usage read for the vendor's 10-minute limit.
DIRECT_RETRY_SECONDS = 610
PLAN_QUIET_SECONDS = 5 * 3600
PLAN_GRACE_SECONDS = 900
WEEKLY_SHIFT_SECONDS = 86400
WEEKLY_DROP_POINTS = 10
FIVE_HOUR_DROP_POINTS = 5
RESET_HOLD_MAX_MINUTES = 300


class AlreadyRunning(Exception):
    """A conflicting operation is already in flight for a tool."""


class PlanConflict(Exception):
    """A manual stoke would overlap a pending plan's target."""

    def __init__(self, target_at: int) -> None:
        super().__init__("stoke would break the plan")
        self.target_at = target_at


class PlanUnreachable(ValueError):
    """A known window stays open beyond the first plan target."""


class RefreshUnsupported(Exception):
    """A tool has no official no-stoke limit reader."""


def first_target(time: str, now: int, tz: ZoneInfo) -> int:
    """Find the next local time strictly after now."""
    date = datetime.fromtimestamp(now, tz).date()
    target = int(datetime.combine(date, local_time.fromisoformat(time), tz).timestamp())
    return target if target > now else following_target(target, time, tz)


def following_target(target_at: int, time: str, tz: ZoneInfo) -> int:
    """Keep the local time on the following calendar day."""
    date = datetime.fromtimestamp(target_at, tz).date() + timedelta(days=1)
    return int(datetime.combine(date, local_time.fromisoformat(time), tz).timestamp())


def plan_due(plan: Plan, five_hour: Window | None, delay: int) -> int:
    """Wait for the target and any known window, or a pending retry."""
    if plan.retry_at is not None:
        return plan.retry_at
    return max(plan.target_at, five_hour.resets_at + delay) if five_hour is not None else plan.target_at


def plan_deadline(plan: Plan, five_hour: Window | None, delay: int) -> int:
    """Allow fifteen minutes beyond the target or known window closure."""
    due = max(plan.target_at, five_hour.resets_at + delay) if five_hour is not None else plan.target_at
    return due + PLAN_GRACE_SECONDS


def effective_next(state: ToolState, settings: Settings) -> tuple[int | None, bool]:
    """Hold automatic attempts in the quiet zone until the plan is due."""
    base = state.next_stoke_at if state.auto_stoke else None
    plan = state.plan
    if plan is None:
        return base, False
    if base is not None and base < plan.target_at - PLAN_QUIET_SECONDS:
        return base, False
    return plan_due(plan, known_window(state), settings.delay_for(state.name)), True


def known_window(state: ToolState) -> Window | None:
    """Exclude a window known to have closed before its stored end."""
    return None if state.window_closed_at is not None else state.five_hour


def reset_next(state: ToolState, now: int, settings: Settings) -> int:
    """Preserve a nearby window end; otherwise restart immediately."""
    assert state.five_hour is not None
    if state.five_hour.resets_at - now <= state.reset_hold_minutes * 60:
        return state.five_hour.resets_at + settings.delay_for(state.name)
    return now


def in_quiet_zone(plan: Plan, now: int) -> bool:
    """Include the quiet zone's start and exclude its target."""
    return plan.target_at - PLAN_QUIET_SECONDS <= now < plan.target_at


def next_after_success(result: StokeResult, delay: int) -> int:
    """Wait for the reported reset plus the configured delay."""
    return result.five_hour.resets_at + delay


def next_after_failure(now: int, consecutive_failures: int, settings: Settings) -> int:
    """Apply the failure backoff, repeating its last value indefinitely."""
    index = min(max(consecutive_failures - 1, 0), len(settings.retry_seconds) - 1)
    return now + settings.retry_seconds[index]


def startup_decision(state: ToolState, now: int, settings: Settings) -> Literal["stoke_now"] | int:
    """Resume verification first, otherwise reuse a future window if known."""
    if state.pending_verify is not None:
        return "stoke_now"
    if state.five_hour is not None and state.five_hour.resets_at > now:
        if state.window_closed_at is not None:
            next_at = reset_next(state, now, settings)
            return "stoke_now" if next_at <= now else next_at
        return state.five_hour.resets_at + settings.delay_for(state.name)
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


def detect_window_reset(previous: Window | None, current_used: float, observed_at: int) -> bool:
    """Detect a substantial usage drop before the stored window ends."""
    return (
        previous is not None and previous.resets_at > observed_at
        and previous.used_percent - current_used >= FIVE_HOUR_DROP_POINTS
    )


def detect_weekly_reset(
    previous: Window | None, previous_at: int | None, current: Window | None, observed_at: int,
) -> bool:
    """Detect a substantial weekly change before the old week ends."""
    return (
        previous is not None and previous_at is not None and current is not None
        and previous.resets_at > observed_at
        and (current.resets_at - previous.resets_at >= WEEKLY_SHIFT_SECONDS
             or previous.used_percent - current.used_percent >= WEEKLY_DROP_POINTS)
    )


def _credit_used(previous: list[ResetCredit], current: list[ResetCredit], observed_at: int) -> bool:
    """Only a missing, unexpired credit counts as spent."""
    available = Counter((credit.title, credit.granted_at, credit.expires_at) for credit in current)
    before = Counter((credit.title, credit.granted_at, credit.expires_at)
                     for credit in previous if credit.expires_at > observed_at)
    return any(count > available[key] for key, count in before.items())


def compute_warning(state: ToolState) -> str | None:
    """Return the highest-priority problem from the latest attempt."""
    if state.stale_resets >= STALE_RESET_LIMIT and not state.auto_stoke:
        return "auto_stopped"
    record = state.last_stoke
    if record is not None and not record.ok and record.error == SIGNED_OUT_ERROR:
        return "signed_out"
    if record is not None and record.opened_new_window is False:
        return "did_not_open_window"
    if record is not None and record.error == "limits unreadable":
        return "limits_unreadable"
    if state.consecutive_failures >= 3:
        return "stoke_failing"
    return None


def third_party_warning(state: ThirdPartyState) -> str | None:
    record = state.last_stoke
    if record is not None:
        if record.error == SIGNED_OUT_ERROR:
            return "signed_out"
        if record.opened_new_window is False:
            return "did_not_open_window"
    if state.warning_reason is not None:
        return state.warning_reason
    return "stoke_failing" if state.consecutive_failures >= 3 else None


class Scheduler:
    """Own tool state, per-tool locks/events, and lifecycle-managed tasks."""

    def __init__(
        self, providers: Mapping[str, Provider], states: dict[str, ToolState],
        settings: Settings, path: Path | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.reset_closes_window = {name: getattr(provider, "reset_closes_window", True) for name, provider in providers.items()}
        self.reset_credits = {name: getattr(provider, "reset_credits", True) for name, provider in providers.items()}
        self.read_modes = {name: getattr(provider, "read_modes", True) for name, provider in providers.items()}
        self.readers = {
            name: cast(LimitReader, provider) for name, provider in providers.items()
            if callable(getattr(provider, "read_limits", None))
        }
        self.states = states
        for name, provider in providers.items():
            if getattr(provider, "third_party_group", False):
                if states[name].third_party is None:
                    states[name].third_party = ThirdPartyState()
            else:
                states[name].third_party = None
        self.settings = settings
        self.path = path if path is not None else settings.data_dir / "state.json"
        self.cycles = CycleLog(settings.data_dir / "cycles.jsonl")
        self.locks = {name: asyncio.Lock() for name in providers}
        self.cli_locks = {
            name: asyncio.Lock() for name, provider in providers.items() if getattr(provider, "serial_cli", False)
        }
        self.events = {name: asyncio.Event() for name in providers}
        self.third_party_events = {name: asyncio.Event() for name, state in states.items() if state.third_party is not None}
        self.refresh_events = {name: asyncio.Event() for name in self.readers}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self.third_party_tasks: dict[str, asyncio.Task[None]] = {}
        self._direct_retries: dict[str, asyncio.Task[None]] = {}
        self._attempts: set[asyncio.Task[object]] = set()
        self._startup_pending = {name: False for name in providers}

    def _persist(self) -> None:
        store.save(self.path, self.states)

    def _wake(self, name: str) -> None:
        self.events[name].set()
        event = self.third_party_events.get(name)
        if event is not None:
            event.set()

    def _schedule_startup(self, name: str) -> None:
        state = self.states[name]
        now = clock.now()
        decision = startup_decision(state, now, self.settings)
        self._startup_pending[name] = decision == "stoke_now"
        state.next_stoke_at = now if decision == "stoke_now" else decision

    def _close_occurrence(self, state: ToolState, result: PlanResult, now: int) -> None:
        plan = state.plan
        if plan is None:
            return
        state.plan_outcome = PlanOutcome(plan.target_at, result, now)
        target = datetime.fromtimestamp(plan.target_at, timezone.utc).isoformat()
        logger.info("tool=%s plan %s target=%s", state.name, result, target)
        if plan.repeat == "once":
            state.plan = None
        else:
            plan.target_at = following_target(plan.target_at, plan.time, self.settings.timezone)
            plan.retry_at = None

    def _expire_plan(self, state: ToolState, now: int) -> bool:
        closed = False
        while state.plan is not None and not state.running:
            if now <= plan_deadline(state.plan, known_window(state), self.settings.delay_for(state.name)):
                break
            self._close_occurrence(state, "missed", now)
            closed = True
        return closed

    async def start(self) -> None:
        """Initialize schedules and launch the supported loops."""
        if self.tasks:
            return
        try:
            self.cycles.prune(clock.now())
        except (OSError, ValueError) as error:
            logger.warning("Cycle log pruning failed: %s", type(error).__name__)
        for name, state in self.states.items():
            state.running = False
            state.refreshing = False
            self._expire_plan(state, clock.now())
            state.warning = compute_warning(state)
            if state.auto_stoke:
                self._schedule_startup(name)
            else:
                state.next_stoke_at = None
            if state.third_party is not None:
                state.third_party.running = False
                self._schedule_third_party(state, clock.now())
        self._persist()
        self.tasks = {name: asyncio.create_task(self._loop(name), name=f"stoke:{name}") for name in self.providers}
        self.refresh_tasks = {
            name: asyncio.create_task(self._refresh_loop(name), name=f"refresh:{name}") for name in self.readers
        }
        self.third_party_tasks = {
            name: asyncio.create_task(self._third_party_loop(name), name=f"stoke:{name}:third-party")
            for name in self.third_party_events
        }

    async def stop(self) -> None:
        """Cancel loops and manual attempts, and wait for their cleanup."""
        tasks = set(self.tasks.values()) | set(self.refresh_tasks.values()) | set(self.third_party_tasks.values())
        tasks.update(self._attempts)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.refresh_tasks.clear()
        self.third_party_tasks.clear()

    def can_refresh(self, name: str) -> bool:
        """Report the provider's optional read capability."""
        return name in self.readers

    def _record_weekly_reset(
        self, state: ToolState, current: Window, observed_at: int, resets: ResetCredits | None = None,
    ) -> bool:
        previous, previous_at = state.weekly, state.limits_at
        if not detect_weekly_reset(previous, previous_at, current, observed_at):
            return False
        assert previous is not None and previous_at is not None
        credits = None if state.resets is None else [replace(credit) for credit in state.resets.credits]
        kind = "early"
        if resets is not None:
            if credits is not None and _credit_used(credits, resets.credits, observed_at):
                kind = "credit"
            credits = None
        state.weekly_resets.insert(0, WeeklyReset(
            uuid4().hex[:12], kind, previous_at, observed_at, previous.used_percent, previous.resets_at,
            current.used_percent, current.resets_at, credits_before=credits,
        ))
        logger.info("tool=%s weekly reset detected kind=%s", state.name, kind)
        return True

    async def _refresh_after_weekly_reset(self, name: str) -> None:
        """Resolve pending classification through the normal usage reader."""
        try:
            await self.refresh_limits(name)
        except (AlreadyRunning, ProviderError):
            pass
        except Exception as error:
            logger.warning("tool=%s refresh failed: %s", name, type(error).__name__)

    def _schedule_direct_retry(self, name: str, base_at: int) -> None:
        """Replace a tool's pending direct read follow-up after token refresh."""
        previous = self._direct_retries.get(name)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(self._retry_direct_read(name, base_at), name=f"direct-retry:{name}")
        self._direct_retries[name] = task
        self._attempts.add(task)

        def done(completed: asyncio.Task[None]) -> None:
            self._attempts.discard(completed)
            if self._direct_retries.get(name) is completed:
                del self._direct_retries[name]

        task.add_done_callback(done)
        logger.info("tool=%s direct read follow-up in %s s", name, DIRECT_RETRY_SECONDS)

    async def _retry_direct_read(self, name: str, base_at: int) -> None:
        """Retry direct usage once, unless another read or changed settings supersede it."""
        await asyncio.sleep(DIRECT_RETRY_SECONDS)
        state = self.states[name]
        for attempt in range(3):
            if (state.read_mode != "direct" or state.auto_refresh.enabled
                    or state.last_read is None or state.last_read.at != base_at):
                logger.info("tool=%s direct read follow-up skipped: settings or last read changed", name)
                return
            try:
                await self.refresh_limits(name, follow_up=True)
            except AlreadyRunning:
                if attempt < 2:
                    await asyncio.sleep(REFRESH_BUSY_RETRY_SECONDS)
            except ProviderError:
                return
            except Exception as error:
                logger.warning("tool=%s refresh failed: %s", name, type(error).__name__)
                return
            else:
                return

    async def refresh_limits(self, name: str, *, follow_up: bool = False) -> ToolState:
        """Update usage and close a stored window after an early reset."""
        if name not in self.readers:
            raise RefreshUnsupported(name)
        state = self.states[name]
        cli_lock = self.cli_locks.get(name)
        if state.refreshing or state.running or (cli_lock is not None and cli_lock.locked()):
            raise AlreadyRunning(name)
        state.refreshing = True
        task = asyncio.current_task()
        if task is not None:
            self._attempts.add(task)
        try:
            try:
                async with cli_lock if cli_lock is not None else nullcontext():
                    snapshot = await self.readers[name].read_limits(state.read_mode)
            except ProviderError as error:
                signed_out = isinstance(error, ReaderSignedOut)
                message = "usage reader signed out" if signed_out else short_error(str(error))
                now = clock.now()
                if signed_out and (state.last_read is None or not state.last_read.signed_out):
                    expiry = "unknown"
                    try:
                        read_expiry = getattr(self.readers[name], "read_login_expires_at", None)
                        value = read_expiry() if callable(read_expiry) else None
                        if type(value) is int and value > 0:
                            expiry = datetime.fromtimestamp(value, timezone.utc).isoformat()
                    except Exception:
                        pass
                    logger.warning("tool=%s usage reader signed out at %s; login refresh expiry %s",
                                   name, datetime.fromtimestamp(now, timezone.utc).isoformat(), expiry)
                state.last_read = ReadRecord(now, False, message, signed_out)
                self._persist()
                if not signed_out:
                    logger.warning("tool=%s refresh failed: %s", name, short_error(str(error)))
                raise
            except Exception as error:
                state.last_read = ReadRecord(clock.now(), False, "unexpected provider error", False)
                self._persist()
                logger.warning("tool=%s refresh failed: %s", name, type(error).__name__)
                raise ProviderError("unexpected provider error") from None
            now = clock.now()
            state.last_read = ReadRecord(now, True, None, False)
            self._apply_limits_observation(state, snapshot, now)
            self._persist()
            if (snapshot.direct_retry and not follow_up and state.read_mode == "direct"
                    and not state.auto_refresh.enabled):
                self._schedule_direct_retry(name, state.last_read.at)
            return state
        finally:
            state.refreshing = False
            if task is not None:
                self._attempts.discard(task)

    def _apply_limits_observation(self, state: ToolState, snapshot: LimitsSnapshot, now: int) -> None:
        """Apply accepted limits from any message-free usage snapshot."""
        name = state.name
        observed = min(snapshot.observed_at, now) if snapshot.observed_at is not None else now
        kept_window = False
        if state.limits_at is None or observed >= state.limits_at:
            self._apply_group_observation(state, snapshot.groups, observed)
            window_reset = detect_window_reset(state.five_hour, snapshot.five_hour.used_percent, observed)
            weekly_reset = False
            if snapshot.weekly is not None and not snapshot.weekly_synthetic:
                weekly_reset = self._record_weekly_reset(state, snapshot.weekly, observed, snapshot.resets)
                state.weekly = snapshot.weekly
            if ((weekly_reset or window_reset) and state.window_closed_at is None
                    and state.five_hour is not None and state.five_hour.resets_at > observed):
                if self.reset_closes_window[name]:
                    state.window_closed_at = observed
                    next_at = reset_next(state, now, self.settings)
                    logger.info("tool=%s window reset detected decision=%s", name, "hold" if next_at > now else "now")
                    if state.auto_stoke:
                        state.next_stoke_at = next_at
                        self._wake(name)
                else:
                    kept_window = True
            if (snapshot.window_confirmed and not state.running and snapshot.five_hour.resets_at > observed
                    and (state.five_hour is None or state.five_hour.resets_at <= observed)):
                state.five_hour = snapshot.five_hour
                state.window_closed_at = None
                pending_cleared = " pending cleared" if state.pending_verify is not None else ""
                state.pending_verify = None
                if state.auto_stoke:
                    state.next_stoke_at = snapshot.five_hour.resets_at + self.settings.delay_for(name)
                    self._wake(name)
                try:
                    resets_at = datetime.fromtimestamp(snapshot.five_hour.resets_at, timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    resets_at = "out-of-range"
                logger.info("tool=%s read confirmed an open window resets_at=%s%s", name, resets_at, pending_cleared)
            elif state.five_hour is not None:
                state.five_hour = Window(
                    snapshot.five_hour.used_percent, state.five_hour.resets_at, state.five_hour.window_minutes,
                )
            state.limits_at = observed
            if snapshot.scoped is not None:
                state.scoped_limits = snapshot.scoped
            if snapshot.plan_name is not None:
                state.plan_name = snapshot.plan_name
            if snapshot.resets is not None:
                for entry in state.weekly_resets:
                    if entry.credits_before is not None:
                        if _credit_used(entry.credits_before, snapshot.resets.credits, observed):
                            entry.kind = "credit"
                        entry.credits_before = None
                state.resets = replace(snapshot.resets, read_at=now)
        if kept_window or state.window_closed_at is not None:
            try:
                resets_at = datetime.fromtimestamp(snapshot.five_hour.resets_at, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                resets_at = "out-of-range"
            if kept_window:
                logger.info("tool=%s reset detected, window kept read five_hour used=%s resets_at=%s", name, snapshot.five_hour.used_percent, resets_at)
            else:
                logger.info("tool=%s read five_hour used=%s resets_at=%s", name, snapshot.five_hour.used_percent, resets_at)

    def _apply_group_observation(self, state: ToolState, groups: list[QuotaGroup] | None, observed: int) -> None:
        if groups is not None:
            state.quota_groups = groups
        tp = state.third_party
        if tp is None:
            return
        group = third_party_quota(groups)
        if group is not None and group.five_hour is not None and not group.five_hour_synthetic:
            if (not tp.running and (tp.five_hour is None or tp.five_hour.resets_at <= observed)
                    and group_window_confirmed(group, observed)):
                self._adopt_third_party_window(state, tp, group.five_hour)
            elif tp.five_hour is not None:
                tp.five_hour = replace(tp.five_hour, used_percent=group.five_hour.used_percent)
        self._wake(state.name)

    def _adopt_third_party_window(self, state: ToolState, tp: ThirdPartyState, window: Window) -> None:
        tp.five_hour = window
        tp.pending_verify = None
        tp.consecutive_failures = 0
        tp.warning = tp.warning_reason = None
        if tp.active(state.auto_stoke):
            tp.next_stoke_at = window.resets_at + self.settings.delay_for(state.name)

    def _known_third_party_window(self, state: ToolState, tp: ThirdPartyState, now: int) -> Window | None:
        if tp.five_hour is not None and tp.five_hour.resets_at > now:
            return tp.five_hour
        group = third_party_quota(state.quota_groups)
        if group is not None and group_window_confirmed(group, max(now, state.limits_at or now)):
            assert group.five_hour is not None
            self._adopt_third_party_window(state, tp, group.five_hour)
            return group.five_hour
        return None

    def _schedule_third_party(self, state: ToolState, now: int) -> None:
        tp = state.third_party
        assert tp is not None
        if not tp.active(state.auto_stoke):
            tp.next_stoke_at = None
            tp.warning = tp.warning_reason = None
        elif not tp.running:
            window = self._known_third_party_window(state, tp, now)
            if tp.pending_verify is not None or window is None:
                tp.next_stoke_at = now
            else:
                tp.next_stoke_at = window.resets_at + self.settings.delay_for(state.name)

    def _set_auto_state(self, state: ToolState, enabled: bool, now: int) -> None:
        changed = state.auto_stoke != enabled
        state.auto_stoke = enabled
        if changed and state.third_party is not None:
            self._schedule_third_party(state, now)
            self._wake(state.name)

    async def _refresh_loop(self, name: str) -> None:
        """Wait while disabled; wake on settings changes and back off failures."""
        event = self.refresh_events[name]
        state = self.states[name]
        failures = 0
        while True:
            event.clear()
            if not state.auto_refresh.enabled:
                await event.wait()
                continue
            seconds = state.auto_refresh.seconds
            delay = seconds
            try:
                await self.refresh_limits(name)
                failures = 0
            except AlreadyRunning:
                delay = REFRESH_BUSY_RETRY_SECONDS
            except ProviderError:
                failures += 1
                delay = min(seconds * 2 ** min(failures, 12), 3600)
            except Exception as error:
                logger.warning("tool=%s refresh failed: %s", name, type(error).__name__)
                failures += 1
                delay = min(seconds * 2 ** min(failures, 12), 3600)
            if event.is_set():
                continue
            try:
                await asyncio.wait_for(event.wait(), delay)
            except TimeoutError:
                pass

    def set_show_scoped(self, name: str, enabled: bool) -> ToolState:
        """Persist model-scoped bar visibility without initiating a read."""
        if name not in self.readers:
            raise RefreshUnsupported(name)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be bool")
        state = self.states[name]
        state.show_scoped = enabled
        self._persist()
        return state

    def set_read_mode(self, name: str, mode: str) -> ToolState:
        """Persist the selected reading path without initiating a read."""
        if name not in self.readers:
            raise RefreshUnsupported(name)
        if not isinstance(mode, str) or mode not in READ_MODES:
            raise ValueError("mode must be direct or cli")
        state = self.states[name]
        state.read_mode = mode
        self._persist()
        return state

    def set_auto_refresh(self, name: str, enabled: bool, seconds: int) -> ToolState:
        """Persist per-tool reading preferences and restart its timer."""
        if name not in self.readers:
            raise RefreshUnsupported(name)
        validate_auto_refresh(enabled, seconds)
        state = self.states[name]
        state.auto_refresh = AutoRefresh(enabled, seconds)
        self._persist()
        self.refresh_events[name].set()
        return state

    def set_reset_hold(self, name: str, minutes: int) -> ToolState:
        """Persist the early-reset threshold and reconsider a waiting stoke."""
        if name not in self.readers or not self.reset_closes_window[name]:
            raise RefreshUnsupported(name)
        if type(minutes) is not int or not 0 <= minutes <= RESET_HOLD_MAX_MINUTES:
            raise ValueError("minutes must be an integer between 0 and 300")
        state = self.states[name]
        state.reset_hold_minutes = minutes
        now = clock.now()
        if (state.window_closed_at is not None and state.auto_stoke and not state.running
                and state.five_hour is not None and state.five_hour.resets_at > now):
            state.next_stoke_at = reset_next(state, now, self.settings)
            self._wake(name)
        self._persist()
        return state

    def set_auto(self, name: str, enabled: bool) -> ToolState:
        """Persist the switch and immediately wake the tool's waiting loop."""
        state = self.states[name]
        changed = state.auto_stoke != enabled
        state.stale_resets = 0
        self._set_auto_state(state, enabled, clock.now())
        state.warning = compute_warning(state)
        if not enabled:
            state.next_stoke_at = None
            self._startup_pending[name] = False
        elif changed:
            self._schedule_startup(name)
        self._persist()
        self._wake(name)
        return state

    def set_third_party(self, name: str, mode: ThirdPartyMode) -> ToolState:
        """Persist the independent quota mode and reconsider its schedule."""
        state = self.states[name]
        tp = state.third_party
        if tp is None:
            raise ValueError("third-party quota not supported")
        if mode not in THIRD_PARTY_MODES:
            raise ValueError("mode must be hide, show or keep_open")
        if tp.mode != mode:
            tp.mode = mode
            self._schedule_third_party(state, clock.now())
        self._persist()
        self._wake(name)
        return state

    def dismiss_weekly_reset(self, name: str, id: str) -> ToolState:
        """Hide one notification on every client, retaining its history."""
        state = self.states[name]
        entry = next((entry for entry in state.weekly_resets if entry.id == id), None)
        if entry is None:
            raise ValueError("unknown weekly reset")
        entry.dismissed = True
        self._persist()
        return state

    def set_plan(
        self, name: str, time: str, repeat: PlanRepeat, auto_after: bool | None,
    ) -> ToolState:
        """Replace a plan, rejecting an unreachable first target without changes."""
        validate_plan(time, repeat, auto_after)
        state = self.states[name]
        target = first_target(time, clock.now(), self.settings.timezone)
        window = known_window(state)
        if window is not None and window.resets_at > target:
            end = datetime.fromtimestamp(window.resets_at, self.settings.timezone).strftime("%H:%M")
            raise PlanUnreachable(f"window open until {end}, so {time} cannot be reached")
        state.plan = Plan(time, repeat, auto_after, target, None)
        self._persist()
        self._wake(name)
        return state

    def cancel_plan(self, name: str) -> ToolState:
        """Remove the plan and wake the ordinary automatic schedule."""
        state = self.states[name]
        state.plan = None
        self._persist()
        self._wake(name)
        return state

    @asynccontextmanager
    async def _main_attempt_lock(self, name: str) -> AsyncIterator[None]:
        lock = self.locks[name]
        if lock.locked():
            raise AlreadyRunning(name)
        cli_lock = self.cli_locks.get(name)
        async with lock:
            task = asyncio.current_task()
            if task is not None:
                self._attempts.add(task)
            try:
                async with cli_lock if cli_lock is not None else nullcontext():
                    yield
            finally:
                if task is not None:
                    self._attempts.discard(task)

    async def stoke_now(self, name: str, trigger: Trigger, *, break_plan: bool = False) -> ToolState:
        """Reject overlapping main attempts, waiting for a serial CLI when needed."""
        follow_up = False
        async with self._main_attempt_lock(name):
            state = self.states[name]
            now = clock.now()
            if name in self.cli_locks and trigger != "manual":
                if self._expire_plan(state, now):
                    self._persist()
                due, planned = effective_next(state, self.settings)
                if due is None or due > now or planned != (trigger == "planned"):
                    return state
            if trigger == "manual" and state.plan is not None and in_quiet_zone(state.plan, now):
                if not break_plan:
                    raise PlanConflict(state.plan.target_at)
                self._close_occurrence(state, "broken", now)
            attempt_plan = state.plan if state.plan is not None and now >= state.plan.target_at else None
            provider = self.providers[name]
            verify = getattr(provider, "verify_stoke", None)
            pending = state.pending_verify if callable(verify) else None
            state.pending_verify = pending
            attempt_at = pending.attempt_at if pending is not None else now
            started = time.perf_counter()
            original = state.five_hour
            closed_at = state.window_closed_at
            previous = original
            if closed_at is not None and previous is not None:
                previous = replace(previous, resets_at=closed_at) if attempt_at < previous.resets_at else None
            auto_stopped = state.stale_resets >= STALE_RESET_LIMIT and not state.auto_stoke
            completed_at = None
            state.running = True
            self._startup_pending[name] = False
            try:
                self._persist()
                try:
                    if pending is not None and callable(verify):
                        result = await verify(pending.delivered)
                    else:
                        result = await provider.stoke()
                except StokeUnverified as error:
                    self._record_failure(state, attempt_at, trigger, str(error), started)
                    if error.snapshot is not None:
                        self._apply_limits_observation(state, error.snapshot, clock.now())
                    if pending is not None:
                        pending.reads += 1
                        if pending.reads >= 3:
                            state.pending_verify = None
                            logger.info("tool=%s stoke verification gave up; next attempt sends a new prompt", name)
                    elif closed_at is not None or original is None or original.resets_at <= attempt_at:
                        state.pending_verify = PendingVerify(attempt_at, error.delivered)
                except LimitsUnreadable:
                    state.pending_verify = None
                    self._record_failure(state, attempt_at, trigger, "limits unreadable", started)
                except ProviderError as error:
                    state.pending_verify = None
                    if isinstance(error, StokeNotDelivered):
                        self._apply_limits_observation(state, error.snapshot, clock.now())
                    message = SIGNED_OUT_ERROR if isinstance(error, StokeSignedOut) else str(error)
                    self._record_failure(state, attempt_at, trigger, message, started)
                except Exception:
                    # Unexpected provider exceptions must not expose their potentially secret text.
                    state.pending_verify = None
                    self._record_failure(state, attempt_at, trigger, "unexpected provider error", started)
                else:
                    state.pending_verify = None
                    completed_at = clock.now()
                    if result.five_hour.resets_at <= completed_at:
                        if result.snapshot is not None:
                            self._apply_limits_observation(state, result.snapshot, completed_at)
                        self._record_failure(
                            state, attempt_at, trigger, "reset time not in the future", started, stale=True,
                        )
                    else:
                        if result.weekly is not None:
                            detected = self._record_weekly_reset(state, result.weekly, completed_at)
                            follow_up = detected and state.resets is not None
                        if (closed_at is not None and original is not None and attempt_at < original.resets_at
                                and abs(result.five_hour.resets_at - original.resets_at) <= 600):
                            opened = None
                            logger.info("tool=%s window was not reset", name)
                        else:
                            opened = opened_new_window(previous, attempt_at, result.five_hour)
                        state.five_hour = result.five_hour
                        state.window_closed_at = None
                        state.weekly = result.weekly
                        if result.scoped is not None:
                            state.scoped_limits = result.scoped
                        observed = completed_at
                        if result.snapshot is not None and result.snapshot.observed_at is not None:
                            observed = min(result.snapshot.observed_at, completed_at)
                        self._apply_group_observation(state, result.groups, observed)
                        state.limits_at = completed_at
                        state.consecutive_failures = 0
                        if auto_stopped and state.stale_resets >= STALE_RESET_LIMIT and not state.auto_stoke:
                            self._set_auto_state(state, True, completed_at)
                        state.stale_resets = 0
                        if attempt_plan is not None and state.plan is attempt_plan:
                            auto_after = attempt_plan.auto_after
                            self._close_occurrence(state, "done", completed_at)
                            if auto_after is not None:
                                self._set_auto_state(state, auto_after, completed_at)
                                state.stale_resets = 0
                        state.last_stoke = StokeRecord(
                            attempt_at, trigger, True, None, opened, result.duration_ms,
                        )
                        state.next_stoke_at = next_after_success(result, self.settings.delay_for(name)) if state.auto_stoke else None
                completed_at = clock.now()
                if attempt_plan is not None and state.plan is attempt_plan and state.last_stoke is not None and not state.last_stoke.ok:
                    now = clock.now()
                    retry = next_after_failure(now, state.consecutive_failures, self.settings)
                    if retry > plan_deadline(attempt_plan, known_window(state), self.settings.delay_for(state.name)):
                        self._close_occurrence(state, "missed", now)
                    else:
                        attempt_plan.retry_at = retry
                self._startup_pending[name] = False
                state.warning = compute_warning(state)
                self._log_stoke(name, state.last_stoke, state.five_hour)
            finally:
                if completed_at is not None and state.last_stoke is not None:
                    record = state.last_stoke
                    try:
                        self.cycles.append(CycleRecord(
                            name, trigger, record.ok, record.error, attempt_at, completed_at,
                            record.duration_ms, self.settings.delay_for(name),
                            previous.resets_at if previous is not None else None,
                            result.five_hour.resets_at if record.ok else None,
                            record.opened_new_window if record.ok else None,
                        ), completed_at)
                    except (OSError, ValueError) as error:
                        logger.warning("tool=%s cycle logging failed: %s", name, type(error).__name__)
                state.running = False
                self._persist()
                self._wake(name)
        if follow_up and self.can_refresh(name) and not state.running and not state.refreshing:
            follow_task = asyncio.create_task(self._refresh_after_weekly_reset(name), name=f"weekly-refresh:{name}")
            self._attempts.add(follow_task)
            follow_task.add_done_callback(self._attempts.discard)
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
            self._set_auto_state(state, False, clock.now())
            state.next_stoke_at = None
            logger.error("tool=%s auto-stoke stopped: repeated stale reset times", state.name)

    def _log_stoke(
        self, name: str, record: StokeRecord | None, window: Window | None, *, verify: bool = False,
    ) -> None:
        if record is None:
            return
        if record.ok and window is not None:
            try:
                detail = "resets_at=" + datetime.fromtimestamp(window.resets_at, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                detail = "resets_at=out-of-range"
        else:
            detail = "error=" + short_error(record.error or "stoke failed")
        logger.info(
            "tool=%s trigger=%s ok=%s duration_ms=%s%s %s",
            name, record.trigger, record.ok, record.duration_ms, " verify=1" if verify else "", detail,
        )

    async def _attempt_third_party(self, name: str) -> None:
        state = self.states[name]
        tp = state.third_party
        assert tp is not None
        cli_lock = self.cli_locks.get(name)
        async with cli_lock if cli_lock is not None else nullcontext():
            now = clock.now()
            if (not tp.active(state.auto_stoke) or tp.running
                    or tp.next_stoke_at is None or tp.next_stoke_at > now):
                return
            pending = tp.pending_verify
            if pending is None:
                window = self._known_third_party_window(state, tp, now)
                if window is not None:
                    tp.next_stoke_at = window.resets_at + self.settings.delay_for(name)
                    self._persist()
                    self._wake(name)
                    return
            tp.running = True
            try:
                await self._stoke_third_party(state, tp, pending, now)
            finally:
                tp.running = False
                self._wake(name)

    async def _stoke_third_party(
        self, state: ToolState, tp: ThirdPartyState, pending: PendingVerify | None, now: int,
    ) -> None:
        name = state.name
        provider = cast(ThirdPartyStoker, self.providers[name])
        previous = tp.five_hour
        started = time.perf_counter()
        attempt_at = now if pending is None else pending.attempt_at
        if pending is None:
            # Persist before sending so a restart verifies before it can send again.
            tp.pending_verify = PendingVerify(attempt_at, delivered=True)
            self._persist()

        result: GroupStokeResult | None = None
        snapshot: LimitsSnapshot | None = None
        reason: ThirdPartyWarningReason | None = None
        error: str | None = None
        reads = 0 if pending is None else pending.reads + 1
        retry_verify = tp.pending_verify
        if pending is not None:
            retry_verify = replace(pending, reads=reads) if reads < 3 else None
        try:
            result = await (provider.stoke_third_party() if pending is None
                            else provider.verify_third_party(pending.delivered))
        except StokeUnverified as unverified:
            snapshot = unverified.snapshot
            error = "prompt sent; usage unverified"
            tp.pending_verify = PendingVerify(attempt_at, unverified.delivered, reads) if reads < 3 else None
            if reads >= 3:
                group = third_party_quota(snapshot.groups) if snapshot is not None else None
                missing = group is None or (group.five_hour is None and not group.five_hour_synthetic)
                reason = "limits_unreadable" if missing else "did_not_open_window"
                error = "limits unreadable" if missing else "prompt sent; window not opened"
        except ReaderSignedOut:
            # A signed-out read cannot resolve whether the prior prompt opened a window.
            tp.pending_verify = retry_verify
            error = SIGNED_OUT_ERROR
        except ProviderError as failure:
            tp.pending_verify = None
            if isinstance(failure, StokeSignedOut):
                error = SIGNED_OUT_ERROR
            elif isinstance(failure, StokeNotDelivered):
                snapshot = failure.snapshot
                error = "Antigravity prompt not delivered"
            elif isinstance(failure, LimitsUnreadable):
                error, reason = "limits unreadable", "limits_unreadable"
            else:
                error = short_error(str(failure))
        except Exception:
            tp.pending_verify = retry_verify
            error = "unexpected provider error"
        else:
            tp.pending_verify = None
            snapshot = result.snapshot
        completed_at = clock.now()
        if snapshot is not None:
            self._apply_limits_observation(state, snapshot, completed_at)
        opened = None
        if result is not None and result.window.resets_at <= completed_at:
            result = None
            error = "reset time not in the future"
        if result is not None:
            tp.five_hour = result.window
            opened = opened_new_window(previous, attempt_at, result.window)
        duration = result.duration_ms if result is not None else int((time.perf_counter() - started) * 1000)
        tp.last_stoke = StokeRecord(attempt_at, "auto", result is not None, error, opened, duration)
        delay = self.settings.delay_for(name)
        if tp.active(state.auto_stoke):
            tp.consecutive_failures = 0 if result is not None else tp.consecutive_failures + 1
            tp.warning_reason = reason
            tp.warning = third_party_warning(tp)
            tp.next_stoke_at = (result.window.resets_at + delay if result is not None
                                else next_after_failure(completed_at, tp.consecutive_failures, self.settings))
        else:
            self._schedule_third_party(state, completed_at)
        cycle_tool = f"{name}-3p"
        self._log_stoke(cycle_tool, tp.last_stoke, tp.five_hour, verify=pending is not None)
        try:
            self.cycles.append(CycleRecord(
                cycle_tool, "auto", result is not None, error, attempt_at, completed_at, duration, delay,
                previous.resets_at if previous is not None else None,
                result.window.resets_at if result is not None else None, opened,
            ), completed_at)
        except (OSError, ValueError) as failure:
            logger.warning("tool=%s cycle logging failed: %s", cycle_tool, type(failure).__name__)
        self._persist()

    async def _third_party_loop(self, name: str) -> None:
        event = self.third_party_events[name]
        state = self.states[name]
        tp = state.third_party
        assert tp is not None
        while True:
            event.clear()
            try:
                next_at = tp.next_stoke_at if tp.active(state.auto_stoke) else None
                if next_at is None:
                    await event.wait()
                    continue
                delay = next_at - clock.now()
                if delay > 0:
                    try:
                        await asyncio.wait_for(event.wait(), delay)
                    except TimeoutError:
                        pass
                else:
                    await self._attempt_third_party(name)
            except Exception as error:
                logger.error("tool=%s-3p loop error: %s", name, type(error).__name__)
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), self.settings.retry_seconds[0])
                except TimeoutError:
                    pass

    async def _loop(self, name: str) -> None:
        event = self.events[name]
        state = self.states[name]
        while True:
            try:
                event.clear()
                now = clock.now()
                if self._expire_plan(state, now):
                    self._persist()
                next_at, planned = effective_next(state, self.settings)
                if next_at is None or state.running:
                    await event.wait()
                    continue
                delay = next_at - now
                if delay > 0:
                    try:
                        await asyncio.wait_for(event.wait(), delay)
                    except TimeoutError:
                        pass
                    continue
                trigger: Trigger = "planned" if planned else "startup" if self._startup_pending[name] else "auto"
                if trigger == "auto" and state.window_closed_at is not None and state.five_hour is not None and now < state.five_hour.resets_at:
                    trigger = "reset"
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
