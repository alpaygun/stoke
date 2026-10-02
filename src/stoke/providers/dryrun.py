"""Development providers that never execute a CLI."""

import asyncio
import time
from typing import Awaitable, Callable

from .. import clock
from ..config import Settings
from ..models import StokeResult, Window
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
