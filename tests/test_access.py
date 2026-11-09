"""Origin identity checks with synthetic RSA tokens and no network access."""

import asyncio
import io
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import FakeProvider
from stoke.access import MAX_CERTS_BYTES, AccessVerifier, fetch_keys
from stoke.api import create_app
from stoke.config import Settings
from stoke.scheduler import Scheduler
from stoke.store import load

TEAM = "example.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "a1b2c3"
DENIED = {"detail": "Cloudflare Access token required"}
UNAVAILABLE = {"detail": "access keys unavailable"}


def jwk(key: rsa.RSAPrivateKey, kid: str = "current") -> dict[str, Any]:
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "alg": "RS256"}


def signed_token(key: rsa.RSAPrivateKey, kid: str = "current", **claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUD, "iat": now - 1, "exp": now + 300, **claims}
    return jwt.encode(
        {name: value for name, value in payload.items() if value is not None},
        key, algorithm="RS256", headers={"kid": kid},
    )


@dataclass
class KeySource:
    """Controllable key endpoint and monotonic clock."""

    data: dict[str, Any]
    at: float = 0.0
    calls: int = 0
    error: Exception | None = None

    def fetch(self, url: str) -> dict[str, Any]:
        assert url == f"{ISSUER}/cdn-cgi/access/certs"
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.data


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey]:
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Access tests must never use the network")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


@pytest.fixture
def access_setup(
    settings_factory: Callable[..., Settings], rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Settings, KeySource, AccessVerifier]:
    settings = settings_factory(STOKE_ACCESS_TEAM_DOMAIN=TEAM, STOKE_ACCESS_AUD=AUD)
    source = KeySource({"keys": [jwk(rsa_keys[0])]})
    verifier = AccessVerifier(settings, fetch=source.fetch, now=lambda: source.at)
    monkeypatch.setattr("stoke.api.AccessVerifier", lambda settings: verifier)
    return settings, source, verifier


def client_for(settings: Settings) -> httpx.AsyncClient:
    providers = {"claude": FakeProvider()}
    scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test")


@pytest.mark.parametrize("empty", ["", " \t"])
def test_disabled_never_creates_verifier_or_fetches(
    empty: str, settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(settings: Settings) -> Any:
        raise AssertionError("Disabled Access must not construct a verifier")
    monkeypatch.setattr("stoke.api.AccessVerifier", forbidden)
    async def scenario() -> None:
        settings = settings_factory(STOKE_ACCESS_TEAM_DOMAIN=empty, STOKE_ACCESS_AUD=empty)
        async with client_for(settings) as client:
            assert (await client.get("/api/state")).status_code == 200
            assert (await client.get("/api/state", headers={"Cf-Access-Jwt-Assertion": "garbage"})).status_code == 200
            assert (await client.post("/api/tools/claude/stoke", headers={"X-Stoke": "1"})).status_code == 200
            response = await client.post("/api/tools/claude/stoke")
            assert response.status_code == 403 and response.json() == {"detail": "X-Stoke: 1 required"}
    asyncio.run(scenario())


@pytest.mark.parametrize("case", [
    "missing", "garbage", "aud", "iss", "expired", "signature", "HS256", "none", "unknown",
    "missing-kid", "future-iat", "missing-exp", "missing-iat", "missing-iss", "missing-aud", "bad-exp",
])
def test_invalid_tokens_have_identical_denial(
    case: str, access_setup: tuple[Settings, KeySource, AccessVerifier],
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    settings, source, _ = access_setup
    claims = {"aud": "other123"} if case == "aud" else {"iss": "https://other.cloudflareaccess.com"} if case == "iss" else {}
    if case == "expired":
        claims["exp"] = int(time.time()) - 120
    if case == "future-iat":
        claims["iat"] = int(time.time()) + 120
    if case.startswith("missing-") and case != "missing-kid":
        claims[case.removeprefix("missing-")] = None
    if case == "bad-exp":
        claims["exp"] = []
    token = signed_token(rsa_keys[1] if case == "signature" else rsa_keys[0], **claims)
    if case in {"HS256", "none", "missing-kid"}:
        payload = {"iss": ISSUER, "aud": AUD, "exp": int(time.time()) + 300, "iat": int(time.time())}
        algorithm = "RS256" if case == "missing-kid" else case
        key = rsa_keys[0] if case == "missing-kid" else "synthetic-hmac-secret-at-least-32-bytes" if case == "HS256" else None
        token = jwt.encode(payload, key, algorithm=algorithm, headers={} if case == "missing-kid" else {"kid": "current"})
    if case == "unknown":
        token = signed_token(rsa_keys[0], kid="unrecognized")
    if case == "garbage":
        token = "not.a.token"
    async def scenario() -> None:
        async with client_for(settings) as client:
            headers = {} if case == "missing" else {"Cf-Access-Jwt-Assertion": token}
            response = await client.get("/api/state", headers=headers)
            assert response.status_code == 403 and response.json() == DENIED
    asyncio.run(scenario())
    if case in {"missing", "garbage", "HS256", "none", "missing-kid"}:
        assert source.calls == 0


def test_valid_access_and_post_guard_order(
    access_setup: tuple[Settings, KeySource, AccessVerifier], rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: 1000)
    settings, source, _ = access_setup
    headers = {"Cf-Access-Jwt-Assertion": signed_token(rsa_keys[0])}
    async def scenario() -> None:
        async with client_for(settings) as client:
            for path in ("/api/state", "/", "/api/cycles"):
                assert (await client.get(path, headers=headers)).status_code == 200
            assert (await client.get("/unknown", headers=headers)).status_code == 404
            response = await client.post("/api/tools/claude/stoke", headers=headers)
            assert response.status_code == 403 and response.json() == {"detail": "X-Stoke: 1 required"}
            response = await client.post("/api/tools/claude/stoke", headers={**headers, "X-Stoke": "1"})
            assert response.status_code == 200 and response.json()["last_stoke"]["ok"]
            for extra in ({}, {"X-Stoke": "1"}):
                response = await client.post("/api/tools/claude/stoke", headers=extra)
                assert response.status_code == 403 and response.json() == DENIED
            response = await client.get("/api/state", headers={"Cookie": f"CF_Authorization={headers['Cf-Access-Jwt-Assertion']}"})
            assert response.status_code == 403 and response.json() == DENIED
    asyncio.run(scenario())
    assert source.calls == 1


@pytest.mark.parametrize("path", ["/", "/api/state", "/api/cycles", "/unknown", "/healthz/", "/healthz/extra"])
@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "HEAD", "PUT", "DELETE"])
def test_all_other_paths_and_methods_require_access(
    path: str, method: str, access_setup: tuple[Settings, KeySource, AccessVerifier],
) -> None:
    settings, source, _ = access_setup
    async def scenario() -> None:
        async with client_for(settings) as client:
            response = await client.request(method, path)
            assert response.status_code == 403
            if method != "HEAD":
                assert response.json() == DENIED
    asyncio.run(scenario())
    assert source.calls == 0


def test_healthz_exempts_access_for_any_method(access_setup: tuple[Settings, KeySource, AccessVerifier]) -> None:
    settings, source, _ = access_setup
    async def scenario() -> None:
        async with client_for(settings) as client:
            response = await client.get("/healthz?check=1")
            assert response.status_code == 200 and response.json() == {"ok": True}
            for method in ("HEAD", "OPTIONS", "PUT", "DELETE"):
                assert (await client.request(method, "/healthz")).status_code == 405
            # The existing POST guard remains unchanged, even on the health path.
            response = await client.post("/healthz")
            assert response.status_code == 403 and response.json() == {"detail": "X-Stoke: 1 required"}
            assert (await client.post("/healthz", headers={"X-Stoke": "1"})).status_code == 405
    asyncio.run(scenario())
    assert source.calls == 0


def test_cache_expiry_and_rotation(
    access_setup: tuple[Settings, KeySource, AccessVerifier], rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    settings, source, _ = access_setup
    headers = {"Cf-Access-Jwt-Assertion": signed_token(rsa_keys[0])}
    async def scenario() -> None:
        async with client_for(settings) as client:
            for _ in range(2):
                assert (await client.get("/api/state", headers=headers)).status_code == 200
            assert source.calls == 1
            source.at = 3601
            assert (await client.get("/api/state", headers=headers)).status_code == 200
            assert source.calls == 2
            source.at += 60
            for kid in ("random1", "random2"):
                response = await client.get("/api/state", headers={"Cf-Access-Jwt-Assertion": signed_token(rsa_keys[0], kid=kid)})
                assert response.status_code == 403 and response.json() == DENIED
            assert source.calls == 3
            source.data = {"keys": [jwk(rsa_keys[1], "rotated")]}
            rotated = {"Cf-Access-Jwt-Assertion": signed_token(rsa_keys[1], kid="rotated")}
            assert (await client.get("/api/state", headers=rotated)).status_code == 403
            assert source.calls == 3
            source.at += 60
            assert (await client.get("/api/state", headers=rotated)).status_code == 200
            assert source.calls == 4
    asyncio.run(scenario())


@pytest.mark.parametrize("cached_age", [None, 3601, 86399, 86400, 86401])
def test_fetch_failure_cache_age_and_safe_warning(
    cached_age: int | None, access_setup: tuple[Settings, KeySource, AccessVerifier],
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey], caplog: pytest.LogCaptureFixture,
) -> None:
    settings, source, _ = access_setup
    token = signed_token(rsa_keys[0])
    caplog.set_level(logging.WARNING, logger="stoke.access")
    async def scenario() -> None:
        async with client_for(settings) as client:
            headers = {"Cf-Access-Jwt-Assertion": token}
            if cached_age is not None:
                assert (await client.get("/api/state", headers=headers)).status_code == 200
                source.at = cached_age
            source.error = OSError(f"private error {TEAM} {AUD} {token}")
            for _ in range(2):
                response = await client.get("/api/state", headers=headers)
                expected = 200 if cached_age is not None and cached_age < 86400 else 503
                assert response.status_code == expected
                if expected == 503:
                    assert response.json() == UNAVAILABLE
            assert source.calls == (1 if cached_age is None else 2)
    asyncio.run(scenario())
    warnings = [record for record in caplog.records if record.name == "stoke.access"]
    assert len(warnings) == 1 and warnings[0].getMessage() == "Access key fetch failed (OSError)"
    assert not warnings[0].exc_info
    assert all(value not in caplog.text for value in (TEAM, AUD, token, "private error"))


def test_concurrent_fetches_are_shared_and_off_event_loop(
    access_setup: tuple[Settings, KeySource, AccessVerifier], rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    _, source, verifier = access_setup
    token = signed_token(rsa_keys[0])
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started, release = asyncio.Event(), threading.Event()
        main_thread = threading.get_ident()
        def fetch(url: str) -> dict[str, Any]:
            assert threading.get_ident() != main_thread
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=5)
            return source.fetch(url)
        verifier.fetch = fetch
        tasks = [asyncio.create_task(verifier.check(token)) for _ in range(12)]
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert not any(task.done() for task in tasks)
        finally:
            release.set()
        assert await asyncio.gather(*tasks) == ["ok"] * 12
        assert source.calls == 1
        source.at = 60
        unknown = [signed_token(rsa_keys[0], kid=f"random{index}") for index in range(12)]
        assert await asyncio.gather(*(verifier.check(token) for token in unknown)) == ["denied"] * 12
        assert source.calls == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [{}, {"keys": []}, {"keys": "invalid"}, {"keys": [{"kty": "invalid"}]}, {"keys": [None]}])
def test_unusable_key_sets_are_unavailable(
    data: dict[str, Any], access_setup: tuple[Settings, KeySource, AccessVerifier],
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPrivateKey],
) -> None:
    _, source, verifier = access_setup
    source.data = data
    assert asyncio.run(verifier.check(signed_token(rsa_keys[0]))) == "unavailable"


@pytest.mark.parametrize("size", [MAX_CERTS_BYTES, MAX_CERTS_BYTES + 1])
def test_fetch_keys_timeout_and_size_bound(size: int, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"keys": []}' + b" " * (size - len(b'{"keys": []}'))
    response = io.BytesIO(body)
    def open_stub(url: str, *, timeout: int) -> io.BytesIO:
        assert url == f"{ISSUER}/cdn-cgi/access/certs" and timeout == 5
        return response
    monkeypatch.setattr("urllib.request.urlopen", open_stub)
    if size > MAX_CERTS_BYTES:
        with pytest.raises(ValueError, match="response too large"):
            fetch_keys(f"{ISSUER}/cdn-cgi/access/certs")
    else:
        assert fetch_keys(f"{ISSUER}/cdn-cgi/access/certs") == {"keys": []}


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"null"])
def test_fetch_keys_rejects_invalid_json(body: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(body))
    with pytest.raises(ValueError):
        fetch_keys(f"{ISSUER}/cdn-cgi/access/certs")
