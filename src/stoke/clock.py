"""Shared wall clock, replaceable in tests."""

import time


def now() -> int:
    """Return the current Unix time in seconds."""
    return int(time.time())
