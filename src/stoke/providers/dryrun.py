"""Development providers that never execute a CLI."""

import asyncio
import time
from typing import Awaitable, Callable

from .. import clock
from ..config import Settings
from ..models import GroupStokeResult, LimitsSnapshot, QuotaGroup, ResetCredit, ResetCredits, ScopedLimit, StokeResult, Window
from .base import ProviderError, StokeNotDelivered, StokeUnverified


class DryRunProvider:
    """Return synthetic windows after a brief simulated command."""

    def __init__(
        self, name: str, label: str, settings: Settings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *, auth_labels: dict[str, dict[str, str]] | None = None,
        reset_closes_window: bool = True, reset_credits: bool = True, read_modes: bool = True,
        read_login_expiry: bool = False, serial_cli: bool = False, third_party_group: bool = False,
    ) -> None:
        self.read_login_expiry = read_login_expiry
        self._login_expires_at = clock.now() + 25 * 86400 if read_login_expiry else None
        self.name = name
        self.label = label
        self.settings = settings
        self.sleep = sleep
        self.reset_closes_window = reset_closes_window
        self.reset_credits = reset_credits
        self.read_modes = read_modes
        self.serial_cli = serial_cli
        self.third_party_group = third_party_group
        self.auth_labels = {key: dict(value) for key, value in (auth_labels or {}).items()}

    async def stoke(self) -> StokeResult:
        """Simulate a stoke, optionally failing the configured tool."""
        started = time.perf_counter()
        await self.sleep(1.5)
        if self.name in self.settings.dry_run_fail:
            raise ProviderError("dry-run failure")
        now = clock.now()
        window = Window(1.0, now + self.settings.dry_run_window_seconds, 300)
        duration = int((time.perf_counter() - started) * 1000)
        return StokeResult(window, Window(25.0, now + 7 * 86400, 10080), duration)


class DryRunReader(DryRunProvider):
    """Simulate reads only for providers with the real capability."""

    def read_login_expires_at(self) -> int | None:
        """Return a stable synthetic browser login expiry when supported."""
        return self._login_expires_at

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        """Return synthetic usage and mode-appropriate reset credits."""
        await self.sleep(1.0)
        if self.name in self.settings.dry_run_fail:
            raise ProviderError("dry-run failure")
        now = clock.now()
        five_hour = Window(3.0, now + self.settings.dry_run_window_seconds, 300)
        weekly = Window(27.0, now + 7 * 24 * 3600, 10080)
        credits = [
            ResetCredit("Full reset (Weekly + 5 hr)", now + offset - 30 * 86400, now + offset)
            for offset in (2 * 86400 + 5000, 18 * 86400, 26 * 86400)
        ]
        resets = ResetCredits(3, credits, 0)
        if self.name == "claude":
            resets = None if mode == "cli" else ResetCredits(2, [
                ResetCredit("Fable reset", None, now + 4 * 86400, True),
                ResetCredit("Full reset", None, now + 27 * 86400, True),
            ], 0)
        return LimitsSnapshot(
            five_hour, weekly, resets,
            scoped=[ScopedLimit("Fable", 23.0, now + 7 * 86400)] if mode == "direct" and self.name == "claude" else None,
            plan_name=("Pro" if self.name == "claude" else "Plus") if mode == "direct" else None,
        )


class DryRunAntigravity(DryRunReader):
    """Simulate Antigravity's two independent windows and shared reads."""

    _gemini_window: Window | None = None
    _third_party_window: Window | None = None
    _third_party_week: Window | None = None
    _gemini_week_end = 0

    async def stoke(self) -> StokeResult:
        result = await super().stoke()
        self._gemini_window = result.five_hour
        snapshot = self._antigravity_snapshot(clock.now(), 25.0)
        return StokeResult(
            result.five_hour, snapshot.weekly, result.duration_ms,
            scoped=snapshot.scoped, groups=snapshot.groups, snapshot=snapshot,
        )

    async def read_limits(self, mode: str = "direct") -> LimitsSnapshot:
        await self.sleep(1.0)
        if self.name in self.settings.dry_run_fail:
            raise ProviderError("dry-run failure")
        return self._antigravity_snapshot(clock.now(), 27.0)

    def _antigravity_snapshot(self, now: int, weekly_used: float) -> LimitsSnapshot:
        window = self._gemini_window
        gemini_open = window is not None and window.resets_at > now
        if window is None or window.resets_at <= now:
            window = Window(0.0, now + self.settings.dry_run_window_seconds, 300)
        third_hour = self._third_party_window
        third_week = self._third_party_week
        if third_hour is not None and third_hour.resets_at <= now:
            third_hour = None
        if third_week is not None and third_week.resets_at <= now:
            third_week = None
        if self._gemini_week_end <= now:
            self._gemini_week_end = now + 7 * 86400
        return LimitsSnapshot(
            window, Window(weekly_used, self._gemini_week_end, 10080), None, observed_at=now,
            scoped=[ScopedLimit("Argon", 9.0, now + 6 * 86400)],
            groups=[QuotaGroup("third_party", third_hour, third_week, third_hour is None, third_week is None)],
            window_confirmed=gemini_open,
        )

    async def stoke_third_party(self) -> GroupStokeResult:
        started = time.perf_counter()
        await self.sleep(1.5)
        if self.name in self.settings.dry_run_fail:
            raise ProviderError("dry-run failure")
        now = clock.now()
        self._third_party_window = Window(2.0, now + self.settings.dry_run_window_seconds, 300)
        if self._third_party_week is None or self._third_party_week.resets_at <= now:
            self._third_party_week = Window(1.0, now + 7 * 86400, 10080)
        return GroupStokeResult(
            self._antigravity_snapshot(now, 27.0), self._third_party_window,
            int((time.perf_counter() - started) * 1000),
        )

    async def verify_third_party(self, delivered: bool) -> GroupStokeResult:
        started = time.perf_counter()
        await self.sleep(1.0)
        if self.name in self.settings.dry_run_fail:
            raise StokeUnverified("prompt sent; usage unverified", delivered=delivered)
        now = clock.now()
        snapshot = self._antigravity_snapshot(now, 27.0)
        window = self._third_party_window
        if window is not None and window.resets_at > now:
            return GroupStokeResult(snapshot, window, int((time.perf_counter() - started) * 1000))
        if delivered:
            raise StokeUnverified("prompt sent; usage unverified", delivered=True, snapshot=snapshot)
        raise StokeNotDelivered("Antigravity prompt not delivered", snapshot=snapshot)
