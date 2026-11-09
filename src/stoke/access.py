"""Optional Cloudflare Access identity verification with bounded key caching."""

import asyncio
import json
import logging
import time
import urllib.request
from http.client import HTTPException
from typing import Any, Callable, Literal

import jwt

from .config import Settings

logger = logging.getLogger(__name__)

MAX_CERTS_BYTES = 256 * 1024
REFRESH_SECONDS = 3600
RETRY_SECONDS = 60
MAX_STALE_SECONDS = 24 * 3600


def fetch_keys(url: str) -> dict[str, Any]:
    """Fetch a bounded JSON document; callers run this outside the event loop."""
    with urllib.request.urlopen(url, timeout=5) as response:
        body = response.read(MAX_CERTS_BYTES + 1)
    if len(body) > MAX_CERTS_BYTES:
        raise ValueError("access key response too large")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("access key response must be an object")
    return data


class AccessVerifier:
    """Verify RS256 tokens using one shared, refreshable set of Access keys."""

    def __init__(
        self, settings: Settings, *,
        fetch: Callable[[str], dict[str, Any]] = fetch_keys,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = f"https://{settings.access_team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.aud = settings.access_aud
        self.fetch = fetch
        self.now = now
        self._keys: jwt.PyJWKSet | None = None
        self._fetched_at = 0.0
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    def _fetch_key_set(self) -> jwt.PyJWKSet:
        data = self.fetch(self.certs_url)
        if not isinstance(data, dict):
            raise ValueError("access key response must be an object")
        return jwt.PyJWKSet.from_dict(data)

    async def _key_set(self, kid: str) -> jwt.PyJWKSet | None:
        async with self._lock:
            now = self.now()
            needs_refresh = (
                self._keys is None or now - self._fetched_at > REFRESH_SECONDS
                or not any(key.key_id == kid for key in self._keys.keys)
            )
            # Throttle failed refreshes as well as unknown kids, including waiters.
            can_refresh = self._last_attempt is None or now - self._last_attempt >= RETRY_SECONDS
            if needs_refresh and can_refresh:
                self._last_attempt = now
                try:
                    keys = await asyncio.to_thread(self._fetch_key_set)
                except (OSError, HTTPException, ValueError, TypeError, jwt.PyJWTError) as error:
                    logger.warning("Access key fetch failed (%s)", type(error).__name__)
                else:
                    self._keys = keys
                    self._fetched_at = self.now()
            if self._keys is None or self.now() - self._fetched_at >= MAX_STALE_SECONDS:
                return None
            return self._keys

    async def check(self, token: str | None) -> Literal["ok", "denied", "unavailable"]:
        """Return a small result without exposing identity data or failure details."""
        if not token:
            return "denied"
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return "denied"
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
            return "denied"
        keys = await self._key_set(kid)
        if keys is None:
            return "unavailable"
        key = next((key for key in keys.keys if key.key_id == kid), None)
        if key is None or key.algorithm_name != "RS256" or key.public_key_use not in (None, "sig"):
            return "denied"
        try:
            jwt.decode(
                token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                leeway=30, options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            return "denied"
        return "ok"
