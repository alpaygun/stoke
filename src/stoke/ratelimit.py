"""In-memory sliding-window limits for accepted attempts per key."""

from collections import deque

RATE_WINDOW_SECONDS = 3600
MANUAL_STOKES_PER_WINDOW = 6
REFRESHES_PER_WINDOW = 30


class SlidingWindowLimiter:
    """Track accepted timestamps per key; callers use one asyncio event loop."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[int]] = {}

    def acquire(self, key: str, now: int) -> int | None:
        """Record an allowed attempt or return the time the next slot frees."""
        attempts = self._attempts.setdefault(key, deque())
        while attempts and attempts[0] <= now - self.window_seconds:
            attempts.popleft()
        if len(attempts) >= self.limit:
            return attempts[0] + self.window_seconds
        attempts.append(now)
        return None

    def release(self, key: str, at: int) -> None:
        """Uncount one matching attempt if it never reached a command."""
        attempts = self._attempts.get(key)
        if attempts is not None:
            try:
                attempts.remove(at)
            except ValueError:
                pass
