"""Development providers that never execute a CLI."""

import asyncio
import time
from typing import Awaitable, Callable

from .. import clock
from ..config import Settings
from ..models import LimitsSnapshot, ResetCredit, ResetCredits, StokeResult, Window
from .base import ProviderError


class DryRunProvider:
    """Return synthetic windows after a brief simulated command."""

    def __init__(
        self, name: str, label: str, settings: Settings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.label = label
        self.settings = settings
        self.sleep = sleep

    async def stoke(self) -> StokeResult:
        """Simulate a stoke, optionally failing the configured tool."""
        started = time.perf_counter()
        await self.sleep(1.5)
        if self.name in self.settings.dry_run_fail:
            raise ProviderError("dry-run failure")
        now = clock.now()
        return StokeResult(
            Window(1.0, now + self.settings.dry_run_window_seconds, 300),
            Window(25.0, now + 7 * 24 * 3600, 10080),
            int((time.perf_counter() - started) * 1000),
        )


class DryRunReader(DryRunProvider):
    """Simulate reads only for providers with the real capability."""

    async def read_limits(self) -> LimitsSnapshot:
        """Return synthetic usage and three available full resets."""
        await self.sleep(1.0)
        if self.name in self.settings.dry_run_fail:
            raise ProviderError("dry-run failure")
        now = clock.now()
        credits = [
            ResetCredit("Full reset (Weekly + 5 hr)", now + offset - 30 * 86400, now + offset)
            for offset in (2 * 86400 + 5000, 18 * 86400, 26 * 86400)
        ]
        return LimitsSnapshot(
            Window(3.0, now + self.settings.dry_run_window_seconds, 300),
            Window(27.0, now + 7 * 24 * 3600, 10080), ResetCredits(3, credits, 0),
        )
