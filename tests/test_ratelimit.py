"""Sliding-window admission, expiry, and reservation release."""

import pytest

from stoke.ratelimit import SlidingWindowLimiter


def test_allows_up_to_limit_then_returns_oldest_expiry() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=10)
    for now in (100, 101, 102):
        assert limiter.acquire("claude", now) is None
    assert limiter.acquire("claude", 103) == 110


def test_frees_slot_exactly_at_window_boundary() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.acquire("claude", 100) is None
    assert limiter.acquire("claude", 101) is None
    assert limiter.acquire("claude", 109) == 110
    assert limiter.acquire("claude", 110) is None
    assert limiter.acquire("claude", 110) == 111


def test_drops_all_expired_entries() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.acquire("claude", 100) is None
    assert limiter.acquire("claude", 101) is None
    assert limiter.acquire("claude", 111) is None
    assert limiter.acquire("claude", 111) is None
    assert limiter.acquire("claude", 111) == 121


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert limiter.acquire("claude", 100) is None
    assert limiter.acquire("claude", 100) == 110
    assert limiter.acquire("codex", 100) is None


@pytest.mark.parametrize("released_at", [100, 101, 102])
def test_release_removes_matching_entry(released_at: int) -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=10)
    for now in (100, 101, 102):
        assert limiter.acquire("claude", now) is None
    limiter.release("claude", released_at)
    assert limiter.acquire("claude", 103) is None
    assert limiter.acquire("claude", 103) == (111 if released_at == 100 else 110)


def test_release_uncounts_only_one_duplicate() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.acquire("claude", 100) is None
    assert limiter.acquire("claude", 100) is None
    limiter.release("claude", 100)
    assert limiter.acquire("claude", 101) is None
    assert limiter.acquire("claude", 101) == 110


def test_release_unknown_key_or_timestamp_is_noop() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    limiter.release("unknown", 100)
    assert limiter.acquire("claude", 100) is None
    limiter.release("claude", 99)
    assert limiter.acquire("claude", 101) == 110
    limiter.release("claude", 100)
    limiter.release("claude", 100)
    assert limiter.acquire("claude", 102) is None


def test_rejected_acquire_does_not_record_attempt() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.acquire("claude", 100) is None
    assert limiter.acquire("claude", 101) is None
    assert limiter.acquire("claude", 102) == 110
    assert limiter.acquire("claude", 110) is None
    assert limiter.acquire("claude", 111) is None
    assert limiter.acquire("claude", 112) == 120
