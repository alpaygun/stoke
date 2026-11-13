"""Direct usage reads use synthetic credentials and injected HTTP only."""

import asyncio
import io
import json
from dataclasses import asdict
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError

import httpx
import pytest

from conftest import FakeProvider, FakeReader
from stoke.api import create_app
from stoke.config import Settings, load_settings
from stoke.httpget import BODY_LIMIT, HttpGetError, HttpResponse, _NoRedirect, http_get
from stoke.models import LimitsSnapshot, ResetCredit, ResetCredits, Window
from stoke.providers import create_providers
from stoke.providers.base import LimitsUnreadable, ProviderError, ReaderSignedOut
from stoke.providers.claude import ClaudeProvider
from stoke.providers.codex import CodexProvider
from stoke.runner import CommandResult
from stoke.scheduler import RefreshUnsupported, Scheduler, _credit_used
from stoke.store import load, save
from stoke.version import stoke_version

NOW = 1800000000
CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
TOKEN = "synthetic-access-sentinel"
ACCOUNT = "synthetic-account-sentinel"
IDENTITIES = ["identity-sentinel", "grant-sentinel", "account-uuid-sentinel"]
HEADERS = {"X-Stoke": "1"}


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def response(value: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(value).encode())


def usage(**overrides: Any) -> dict[str, Any]:
    return {
        "five_hour": {"utilization": 17.5, "resets_at": iso(NOW + 18000)},
        "seven_day": {"utilization": 45, "resets_at": iso(NOW + 604800)},
        "seven_day_breakdown": {"as_of": iso(NOW - 30)}, **overrides,
    }


def codex_usage(**overrides: Any) -> dict[str, Any]:
    return {"rate_limit": {
        "primary_window": {"used_percent": 17.5, "reset_at": NOW + 18000, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 45, "reset_at": NOW + 604800, "limit_window_seconds": 604800},
        **overrides,
    }, "account_id": ACCOUNT, "user_id": "ignored-user-sentinel", "plan_type": "ignored-plan-sentinel"}


class FakeHttp:
    def __init__(self, *outcomes: HttpResponse | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    async def __call__(self, url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout_s))
        assert self.outcomes, "unexpected HTTP request"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeCli:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> CommandResult:
        self.calls += 1
        return CommandResult(0, json.dumps({"type": "control_response", "response": {
            "request_id": "stoke-usage", "subtype": "success",
            "response": {"rate_limits_available": True, "rate_limits": usage()},
        }}), "", 1, False)


class FakeRpc:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"result": {"rateLimits": {"primary": {
            "usedPercent": 12, "resetsAt": NOW + 18000, "windowDurationMins": 300,
        }}}}


@pytest.fixture(autouse=True)
def fixed_clock_and_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stoke.clock.now", lambda: NOW)
    monkeypatch.setattr("stoke.providers.claude._ineligible_reported", False)
    monkeypatch.setattr("stoke.providers.claude._version_warning_reported", False)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings], tmp_path: Path) -> Settings:
    return settings_factory(STOKE_CLAUDE_CLI_VERSION="2.1.296", STOKE_CLAUDE_READER_DIR=str(tmp_path / "reader"))


def claude_auth(settings: Settings, **overrides: Any) -> Path:
    """Create synthetic fixture data only inside pytest's temporary directory."""
    path = settings.claude_reader_dir / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": TOKEN, "expiresAt": (NOW + 3600) * 1000,
        "refreshTokenExpiresAt": (NOW + 86400) * 1000, **overrides,
    }}))
    return path


def codex_auth(settings: Settings, **overrides: Any) -> Path:
    path = settings.codex_home / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": TOKEN, "account_id": ACCOUNT, **overrides}}))
    return path


def scheduler_for(settings: Settings, provider: Any) -> Scheduler:
    providers = {provider.name: provider}
    return Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)


def test_claude_direct_windows_headers_observed_and_credential_immutability(settings: Settings) -> None:
    path = claude_auth(settings)
    before = path.read_bytes(), path.stat().st_mtime_ns
    http, cli = FakeHttp(response(usage())), FakeCli()
    snapshot = asyncio.run(ClaudeProvider(settings, runner=cli, http=http).read_limits())
    assert snapshot == LimitsSnapshot(Window(17.5, NOW + 18000, 300), Window(45, NOW + 604800, 10080), None, NOW - 30)
    assert http.calls == [(CLAUDE_URL, {
        "Authorization": f"Bearer {TOKEN}", "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": "claude-cli/2.1.296 (external, cli)",
    }, 30)]
    assert cli.calls == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def grant(clears: Any, offset: int, count: Any = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "clears": clears, "resets_left": count, "starts_at": iso(NOW - 100), "ends_at": iso(NOW + offset),
        "type": "full", "label": "Misleading full reset", "id": IDENTITIES[0],
        "grant_id": IDENTITIES[1], "account_uuid": IDENTITIES[2], **overrides,
    }


def test_claude_credits_mapping_duplicates_skips_sort_and_identity_privacy(
    settings: Settings, caplog: pytest.LogCaptureFixture,
) -> None:
    claude_auth(settings)
    grants = [
        grant(["five_hour", "seven_day"], 400),
        grant(["five_hour", "seven_day_overage_included"], 200, 2),
        grant(["five_hour"], 300), grant(["something"], 100),
        grant(["five_hour"], 50, 0), grant(["seven_day"], -10),
        grant(["seven_day"], 10, ends_at="bad timestamp"),
        grant(["five_hour"], 100, True), grant(["five_hour"], 100, "2"), "invalid",
    ]
    http = FakeHttp(response(usage(cedar_ember={"eligible": True, "grants": grants})))
    scheduler = scheduler_for(settings, ClaudeProvider(settings, http=http))
    state = asyncio.run(scheduler.refresh_limits("claude"))
    expected = [
        ResetCredit("Other reset", NOW - 100, NOW + 100),
        ResetCredit("Fable reset", NOW - 100, NOW + 200),
        ResetCredit("Fable reset", NOW - 100, NOW + 200),
        ResetCredit("5-hour reset", NOW - 100, NOW + 300),
        ResetCredit("Full reset", NOW - 100, NOW + 400),
    ]
    assert state.resets == ResetCredits(5, expected, NOW)
    serialized = json.dumps(asdict(state)) + scheduler.path.read_text() + caplog.text
    assert all(sentinel not in serialized for sentinel in [TOKEN, ACCOUNT, *IDENTITIES])


@pytest.mark.parametrize("clears,title", [
    (["five_hour", "five_hour"], "5-hour reset"), ([], "Other reset"),
    (["five_hour", {}], "Other reset"), ("five_hour", "Other reset"),
    (["seven_day", "unknown"], "Full reset"),
])
def test_claude_credit_titles_counts_cap_and_optional_granted(settings: Settings, clears: Any, title: str) -> None:
    claude_auth(settings)
    http = FakeHttp(response(usage(cedar_ember={"eligible": True, "grants": [
        grant(clears, 100, 25, starts_at="bad"),
    ]})))
    snapshot = asyncio.run(ClaudeProvider(settings, http=http).read_limits())
    assert snapshot.resets == ResetCredits(20, [ResetCredit(title, None, NOW + 100)] * 20, 0)


@pytest.mark.parametrize("block,expected", [(None, None), ([], None), ({"eligible": True, "grants": None}, ResetCredits(0, [], 0))])
def test_claude_absent_or_empty_credits(settings: Settings, block: Any, expected: Any) -> None:
    claude_auth(settings)
    snapshot = asyncio.run(ClaudeProvider(settings, http=FakeHttp(response(usage(cedar_ember=block)))).read_limits())
    assert snapshot.resets == expected


def test_claude_ineligible_warning_once_per_process_and_previous_list_kept(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    claude_auth(settings)
    result = response(usage(cedar_ember={"eligible": False, "ineligible_reason": "surface\nreason"}))
    reader = ClaudeProvider(settings, http=FakeHttp(result, result))
    scheduler = scheduler_for(settings, reader)
    previous = ResetCredits(1, [ResetCredit("Full reset", None, NOW + 1000)], NOW - 100)
    scheduler.states["claude"].resets = previous
    asyncio.run(scheduler.refresh_limits("claude"))
    monkeypatch.setattr("stoke.clock.now", lambda: NOW + 600)
    asyncio.run(scheduler.refresh_limits("claude"))
    assert scheduler.states["claude"].resets == previous
    # A second provider instance also shares the process-wide warning flag.
    other = ClaudeProvider(settings, http=FakeHttp(result))
    assert asyncio.run(other.read_limits()).resets is None
    assert [m for m in caplog.messages if "not eligible" in m] == [
        "tool=claude reset credits not eligible (reason=surface reason); keeping previous list",
    ]


@pytest.mark.parametrize("kind,http_calls,cli_calls,exception", [
    ("expired", 0, 1, None), ("soon", 0, 1, None), ("401", 1, 1, None),
    ("429", 1, 0, ProviderError), ("500", 1, 0, ProviderError),
    ("network", 1, 0, ProviderError), ("empty", 0, 0, ReaderSignedOut),
    ("nonstring", 0, 0, ReaderSignedOut), ("missing", 0, 0, ReaderSignedOut),
    ("bad_json", 0, 1, None), ("bad_shape", 0, 1, None), ("unreadable", 0, 1, None),
    ("no_version", 0, 1, None), ("cli", 0, 1, None),
])
def test_claude_fallback_and_failures(
    settings: Settings, kind: str, http_calls: int, cli_calls: int, exception: Any,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from dataclasses import replace
    caplog.set_level("INFO")
    overrides = {"expired": {"expiresAt": NOW * 1000}, "soon": {"expiresAt": (NOW + 60) * 1000},
                 "empty": {"accessToken": ""}, "nonstring": {"accessToken": True}}.get(kind, {})
    path = claude_auth(settings, **overrides) if kind != "missing" else None
    if kind == "bad_json":
        path.write_text("bad json")
    if kind == "bad_shape":
        path.write_text("[]")
    if kind == "unreadable":
        original = Path.read_text
        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == path:
                raise PermissionError("private error sentinel")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read_text)
    if kind == "no_version":
        settings = replace(settings, claude_cli_version="")
    before = (path.read_bytes(), path.stat().st_mtime_ns) if path else None
    http = FakeHttp(HttpGetError("timed out") if kind == "network" else HttpResponse(int(kind) if kind.isdigit() else 200, b"private-body-sentinel"))
    cli = FakeCli()
    reader = ClaudeProvider(settings, runner=cli, http=http)
    if exception:
        with pytest.raises(exception) as error:
            asyncio.run(reader.read_limits())
        if kind == "429":
            assert str(error.value) == "Claude usage endpoint rate limited (HTTP 429)"
        if kind == "500":
            assert str(error.value) == "Claude usage read failed (HTTP 500)"
        assert "private-body-sentinel" not in str(error.value)
    else:
        assert asyncio.run(reader.read_limits("cli" if kind == "cli" else "direct")).five_hour.used_percent == 17.5
    assert len(http.calls) == http_calls and cli.calls == cli_calls
    if path:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert all(secret not in caplog.text for secret in (TOKEN, "private-body-sentinel", "private error sentinel"))


def test_claude_ten_minute_guard_records_failed_attempts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    claude_auth(settings)
    http = FakeHttp(HttpResponse(429, b""), response(usage()))
    reader = ClaudeProvider(settings, http=http)
    with pytest.raises(ProviderError, match="HTTP 429"):
        asyncio.run(reader.read_limits())
    for elapsed, remaining in [(0, 10), (1, 10), (540, 1), (599, 1)]:
        monkeypatch.setattr("stoke.clock.now", lambda: NOW + elapsed)
        with pytest.raises(ProviderError, match=f"try again in {remaining} min"):
            asyncio.run(reader.read_limits())
    assert len(http.calls) == 1
    monkeypatch.setattr("stoke.clock.now", lambda: NOW + 600)
    assert asyncio.run(reader.read_limits()).five_hour.used_percent == 17.5
    assert len(http.calls) == 2


@pytest.mark.parametrize("value", [b"bad", b"\xff", b"[]", b"null", b"{}"])
def test_claude_bad_usage_is_generic(settings: Settings, value: bytes) -> None:
    claude_auth(settings)
    with pytest.raises(LimitsUnreadable, match="^limits unreadable$"):
        asyncio.run(ClaudeProvider(settings, http=FakeHttp(HttpResponse(200, value))).read_limits())


@pytest.mark.parametrize("breakdown", [None, {}, {"as_of": "bad"}])
def test_claude_optional_weekly_and_observed(settings: Settings, breakdown: Any) -> None:
    claude_auth(settings)
    snapshot = asyncio.run(ClaudeProvider(settings, http=FakeHttp(response(usage(seven_day=None, seven_day_breakdown=breakdown)))).read_limits())
    assert snapshot.weekly is None and snapshot.observed_at is None


@pytest.mark.parametrize("expiry,expected", [(None, None), (True, None), (-1, None), (0, None), ("1234", None), (1800000000123, 1800000000)])
def test_login_expiry_safe_and_ms_to_seconds(settings: Settings, expiry: Any, expected: Any) -> None:
    reader = ClaudeProvider(settings)
    assert reader.read_login_expires_at() is None
    claude_auth(settings, refreshTokenExpiresAt=expiry)
    assert reader.read_login_expires_at() == expected


def test_login_expiry_cache_and_change_detection(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    path = claude_auth(settings)
    reader = ClaudeProvider(settings)
    original, reads = Path.read_text, []
    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    assert reader.read_login_expires_at() == NOW + 86400
    assert reader.read_login_expires_at() == NOW + 86400
    assert reads == [path]
    path.write_text('{"claudeAiOauth": {"refreshTokenExpiresAt": 1900000000123}}')
    assert reader.read_login_expires_at() == 1900000000
    assert reads == [path, path]
    path.write_text("{}")
    assert reader.read_login_expires_at() is None
    path.write_text("invalid")
    assert reader.read_login_expires_at() is None
    path.unlink()
    assert reader.read_login_expires_at() is None


def test_codex_direct_windows_headers_credits_and_privacy(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    path = codex_auth(settings)
    before = path.read_bytes(), path.stat().st_mtime_ns
    block = {"available_count": 3, "credits": [
        {"status": "available", "title": "  Full\nreset\u200b ", "granted_at": iso(NOW - 100), "expires_at": iso(NOW + 200), "id": IDENTITIES[0]},
        {"status": "available", "title": 42, "granted_at": "bad", "expires_at": iso(NOW + 100)},
        {"status": "spent", "title": "ignored", "expires_at": iso(NOW + 10)},
        {"status": "available", "expires_at": "invalid"},
    ]}
    http, rpc = FakeHttp(response(codex_usage()), response(block)), FakeRpc()
    provider = CodexProvider(settings, rpc=rpc, http=http)
    snapshot = asyncio.run(provider.read_limits())
    assert snapshot == LimitsSnapshot(Window(17.5, NOW + 18000, 300), Window(45, NOW + 604800, 10080), ResetCredits(3, [
        ResetCredit("Limit reset", None, NOW + 100), ResetCredit("Fullreset", NOW - 100, NOW + 200),
    ], 0))
    headers = {"Authorization": f"Bearer {TOKEN}", "ChatGPT-Account-Id": ACCOUNT,
               "Accept": "application/json", "User-Agent": f"stoke/{stoke_version()}"}
    assert headers["User-Agent"].startswith("stoke/")
    assert http.calls == [(CODEX_URL, headers, 30), (CREDITS_URL, headers, 30)]
    assert rpc.calls == 0 and (path.read_bytes(), path.stat().st_mtime_ns) == before
    scheduler = scheduler_for(settings, provider)
    provider.http = FakeHttp(response(codex_usage()), response(block))
    asyncio.run(scheduler.refresh_limits("codex"))
    serialized = json.dumps(asdict(snapshot)) + scheduler.path.read_text() + caplog.text
    assert all(sentinel not in serialized for sentinel in [TOKEN, ACCOUNT, *IDENTITIES, "ignored-user-sentinel", "ignored-plan-sentinel"])


@pytest.mark.parametrize("outcome", [
    HttpResponse(401, b"private-body-sentinel"), HttpResponse(429, b""), HttpResponse(500, b""),
    HttpGetError("network error"), HttpResponse(200, b"private-body-sentinel"), HttpResponse(200, b"\xff"),
    response([]), response({}), response({"available_count": True}), response({"available_count": -1}),
])
def test_codex_credits_failure_keeps_usage(settings: Settings, outcome: Any, caplog: pytest.LogCaptureFixture) -> None:
    codex_auth(settings)
    rpc, http = FakeRpc(), FakeHttp(response(codex_usage()), outcome)
    snapshot = asyncio.run(CodexProvider(settings, rpc=rpc, http=http).read_limits())
    assert snapshot.resets is None and snapshot.five_hour.used_percent == 17.5
    assert rpc.calls == 0 and len(http.calls) == 2
    assert "reset credits read failed" in caplog.text and "private-body-sentinel" not in caplog.text


@pytest.mark.parametrize("kind", ["missing", "401", "cli", "invalid_json", "shape", "empty_token", "empty_account", "unreadable"])
def test_codex_fallbacks(settings: Settings, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if kind != "missing":
        path = codex_auth(settings, **({"access_token": ""} if kind == "empty_token" else {"account_id": ""} if kind == "empty_account" else {}))
        if kind in {"invalid_json", "shape"}:
            path.write_text("invalid" if kind == "invalid_json" else "[]")
        if kind == "unreadable":
            original = Path.read_text
            def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
                if self == path:
                    raise PermissionError("private error sentinel")
                return original(self, *args, **kwargs)
            monkeypatch.setattr(Path, "read_text", read_text)
    http, rpc = FakeHttp(HttpResponse(401, b"private-body-sentinel")) if kind == "401" else FakeHttp(), FakeRpc()
    snapshot = asyncio.run(CodexProvider(settings, rpc=rpc, http=http).read_limits("cli" if kind == "cli" else "direct"))
    assert snapshot.five_hour.used_percent == 12 and rpc.calls == 1
    assert len(http.calls) == (1 if kind == "401" else 0)


@pytest.mark.parametrize("outcome,error_type,message", [
    (HttpResponse(429, b"private-body-sentinel"), ProviderError, "Codex usage endpoint rate limited (HTTP 429)"),
    (HttpResponse(503, b"private-body-sentinel"), ProviderError, "Codex usage read failed (HTTP 503)"),
    (HttpGetError("network error"), ProviderError, "Codex usage read failed: network error"),
    (HttpResponse(200, b"private-body-sentinel"), LimitsUnreadable, "limits unreadable"),
    (response([]), LimitsUnreadable, "limits unreadable"),
])
def test_codex_usage_failure_no_cli(settings: Settings, outcome: Any, error_type: Any, message: str) -> None:
    codex_auth(settings)
    http, rpc = FakeHttp(outcome), FakeRpc()
    with pytest.raises(error_type) as error:
        asyncio.run(CodexProvider(settings, rpc=rpc, http=http).read_limits())
    assert str(error.value) == message and rpc.calls == 0 and len(http.calls) == 1


@pytest.mark.parametrize("field,value", [("used_percent", True), ("used_percent", 101), ("reset_at", "123"), ("reset_at", True)])
def test_codex_bad_window(settings: Settings, field: str, value: Any) -> None:
    codex_auth(settings)
    payload = codex_usage()
    payload["rate_limit"]["primary_window"][field] = value
    with pytest.raises(LimitsUnreadable):
        asyncio.run(CodexProvider(settings, http=FakeHttp(response(payload))).read_limits())


@pytest.mark.parametrize("seconds", [None, 0, -1, True, "18000"])
def test_codex_optional_duration_and_weekly(settings: Settings, seconds: Any) -> None:
    codex_auth(settings)
    payload = codex_usage(secondary_window=None)
    payload["rate_limit"]["primary_window"]["limit_window_seconds"] = seconds
    snapshot = asyncio.run(CodexProvider(settings, http=FakeHttp(response(payload), response({"available_count": 0}))).read_limits())
    assert snapshot.five_hour.window_minutes is None and snapshot.weekly is None


@pytest.mark.parametrize("value", ["", " \t", "2.1.296", " 2.1.296 "])
def test_cli_version_config(value: str) -> None:
    assert load_settings({"STOKE_CLAUDE_CLI_VERSION": value}).claude_cli_version == value.strip()


@pytest.mark.parametrize("value", ["2.1", "2.1.296-extra", "２.1.296", "2.1.296\ninvalid", "2x1x296"])
def test_cli_version_config_invalid(value: str) -> None:
    with pytest.raises(ValueError, match="^STOKE_CLAUDE_CLI_VERSION must be X.Y.Z$"):
        load_settings({"STOKE_CLAUDE_CLI_VERSION": value})


@pytest.mark.parametrize("mode", [None, "bad", [], {}, False, "cli", "direct"])
def test_store_read_mode_tolerant_and_round_trip(settings: Settings, mode: Any, caplog: pytest.LogCaptureFixture) -> None:
    scheduler = scheduler_for(settings, FakeReader())
    state = scheduler.states["codex"]
    state.five_hour = Window(42, NOW + 100, 300)
    save(scheduler.path, scheduler.states)
    data = json.loads(scheduler.path.read_text())
    if mode is None:
        del data["codex"]["read_mode"]
    else:
        data["codex"]["read_mode"] = mode
    scheduler.path.write_text(json.dumps(data))
    caplog.clear()
    restored = load(scheduler.path, settings, scheduler.providers)["codex"]
    assert restored.read_mode == (mode if mode in ("direct", "cli") else "direct")
    assert restored.five_hour == state.five_hour and "using defaults" not in caplog.text


@pytest.mark.parametrize("seconds,expected", [(60, 600), (300, 600), (600, 600), (3600, 3600), (86400, 3600), (None, 600), (629, 600), (630, 660)])
def test_store_refresh_interval_migration(settings: Settings, seconds: Any, expected: int) -> None:
    scheduler = scheduler_for(settings, FakeReader())
    save(scheduler.path, scheduler.states)
    data = json.loads(scheduler.path.read_text())
    data["codex"]["auto_refresh"] = {"enabled": True, **({} if seconds is None else {"seconds": seconds})}
    scheduler.path.write_text(json.dumps(data))
    restored = load(scheduler.path, settings, scheduler.providers)["codex"]
    assert restored.auto_refresh.enabled and restored.auto_refresh.seconds == expected


def test_scheduler_passes_read_mode_without_read_on_switch(settings: Settings) -> None:
    reader = FakeReader()
    scheduler = scheduler_for(settings, reader)
    assert scheduler.set_read_mode("codex", "cli").read_mode == "cli"
    assert reader.read_calls == 0
    assert load(scheduler.path, settings, scheduler.providers)["codex"].read_mode == "cli"
    asyncio.run(scheduler.refresh_limits("codex"))
    scheduler.set_read_mode("codex", "direct")
    asyncio.run(scheduler.refresh_limits("codex"))
    assert reader.read_modes == ["cli", "direct"]
    for invalid in [None, [], {}, 1, "bad"]:
        with pytest.raises(ValueError, match="^mode must be direct or cli$"):
            scheduler.set_read_mode("codex", invalid)
    with pytest.raises(RefreshUnsupported):
        scheduler_for(settings, FakeProvider()).set_read_mode("claude", "cli")


@pytest.mark.parametrize("expiry_kind,expected", [("valid", iso(NOW + 86400)), ("absent", "unknown"), ("raises", "unknown")])
def test_signed_out_transition_logs_once_and_after_success(
    settings: Settings, expiry_kind: str, expected: str, caplog: pytest.LogCaptureFixture,
) -> None:
    reader = FakeReader("claude")
    if expiry_kind != "absent":
        def expiry() -> int:
            if expiry_kind == "raises":
                raise ValueError("private error sentinel")
            return NOW + 86400
        reader.read_login_expires_at = expiry
    scheduler = scheduler_for(settings, reader)
    caplog.clear()
    success = reader.read_result
    for result in [ReaderSignedOut("unused error text"), ReaderSignedOut("unused error text"), success, ReaderSignedOut("unused error text")]:
        reader.read_result = result
        if isinstance(result, Exception):
            with pytest.raises(ReaderSignedOut):
                asyncio.run(scheduler.refresh_limits("claude"))
        else:
            asyncio.run(scheduler.refresh_limits("claude"))
    assert caplog.messages == [f"tool=claude usage reader signed out at {iso(NOW)}; login refresh expiry {expected}"] * 2
    assert all(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("remaining,expected", [(1, "credit"), (2, "early")])
def test_duplicate_credit_spending_in_weekly_reset(settings: Settings, remaining: int, expected: str) -> None:
    credit = ResetCredit("Fable reset", NOW - 100, NOW + 1000)
    previous = [credit, credit]
    assert _credit_used(previous, [credit] * remaining, NOW) is (remaining == 1)
    assert not _credit_used(previous, [], NOW + 1000)
    reader = FakeReader("claude")
    reader.read_result = LimitsSnapshot(Window(5, NOW + 18000, 300), Window(5, NOW + 604800, 10080), ResetCredits(remaining, [credit] * remaining, 0))
    scheduler = scheduler_for(settings, reader)
    state = scheduler.states["claude"]
    state.weekly, state.limits_at = Window(70, NOW + 604800, 10080), NOW - 50
    state.resets = ResetCredits(2, previous, NOW - 50)
    asyncio.run(scheduler.refresh_limits("claude"))
    assert state.weekly_resets[0].kind == expected


def test_api_modes_validation_refresh_minimum_and_dry_login_expiry(settings: Settings) -> None:
    async def scenario() -> None:
        providers = create_providers(settings)
        providers["codex"] = FakeProvider("codex")
        scheduler = Scheduler(providers, load(settings.data_dir / "state.json", settings, providers), settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(scheduler, settings)), base_url="http://test") as client:
            tools = (await client.get("/api/state")).json()["tools"]
            assert tools["claude"]["read_mode"] == "direct"
            assert tools["claude"]["auth"]["read"]["expires_at"] == NOW + 25 * 86400
            for mode in ["cli", "direct"]:
                result = await client.post("/api/tools/claude/read-mode", headers=HEADERS, json={"mode": mode})
                assert result.status_code == 200 and result.json()["read_mode"] == mode
                assert load(scheduler.path, settings, providers)["claude"].read_mode == mode
            for body in [{"mode": "bad"}, {"mode": 1}, {"mode": []}, {"mode": None}, {"mode": "cli", "extra": True}, {}, [], None]:
                assert (await client.post("/api/tools/claude/read-mode", headers=HEADERS, json=body)).status_code == 422
            assert (await client.post("/api/tools/claude/read-mode", headers=HEADERS, content="invalid")).status_code == 422
            assert (await client.post("/api/tools/claude/read-mode", json={"mode": "cli"})).status_code == 403
            for name, detail in [("unknown", "unknown tool"), ("codex", "refresh not supported")]:
                result = await client.post(f"/api/tools/{name}/read-mode", headers=HEADERS, json={"mode": "cli"})
                assert result.status_code == 404 and result.json() == {"detail": detail}
            for seconds in [599, 540, 600]:
                result = await client.post("/api/tools/claude/auto-refresh", headers=HEADERS, json={"enabled": True, "seconds": seconds})
                assert result.status_code == (200 if seconds == 600 else 422)
            assert scheduler.states["claude"].last_read is None
    asyncio.run(scenario())


def test_dry_run_modes_and_stable_login_expiry(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    providers = create_providers(settings)
    async def no_wait(seconds: float) -> None:
        pass
    for provider in providers.values():
        provider.sleep = no_wait
    monkeypatch.setattr("stoke.clock.now", lambda: NOW + 60)
    assert providers["claude"].read_login_expires_at() == NOW + 25 * 86400
    assert providers["codex"].read_login_expires_at() is None
    direct = asyncio.run(providers["claude"].read_limits())
    assert direct.resets == ResetCredits(2, [ResetCredit("Fable reset", None, NOW + 60 + 4 * 86400), ResetCredit("Full reset", None, NOW + 60 + 27 * 86400)], 0)
    assert asyncio.run(providers["claude"].read_limits("cli")).resets is None
    assert asyncio.run(providers["codex"].read_limits()).resets == asyncio.run(providers["codex"].read_limits("cli")).resets


@pytest.mark.parametrize("status,size", [(200, 10), (200, BODY_LIMIT), (302, 10), (401, 10), (429, 10), (500, 10)])
def test_http_get_status_bound_method_and_thread(
    status: int, size: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    main_thread = threading.get_ident()
    calls = []
    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            assert threading.get_ident() != main_thread
            calls.append((request.full_url, request.get_method(), dict(request.header_items()), timeout))
            body = io.BytesIO(b"x" * size)
            if status != 200:
                raise HTTPError(request.full_url, status, "private reason", {}, body)
            class Response:
                fp = body
                def __init__(self) -> None:
                    self.status = status
                def __enter__(self) -> Any:
                    return self
                def __exit__(self, *args: Any) -> None:
                    body.close()
                def read(self, count: int) -> bytes:
                    assert count == BODY_LIMIT + 1
                    return body.read(count)
            return Response()
    def opener(handler: Any) -> Opener:
        assert isinstance(handler, _NoRedirect)
        assert handler.redirect_request(None, None, 302, None, {}, CODEX_URL) is None
        return Opener()
    monkeypatch.setattr("stoke.httpget.build_opener", opener)
    result = asyncio.run(http_get(CLAUDE_URL, {"Authorization": f"Bearer {TOKEN}"}, 3))
    assert result == HttpResponse(status, b"x" * size)
    assert calls == [(CLAUDE_URL, "GET", {"Authorization": f"Bearer {TOKEN}"}, 3)]


@pytest.mark.parametrize("status", [200, 401])
def test_http_get_body_cap_on_success_and_errors(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            # HTTPError is also a file-like response, even for this synthetic 200.
            raise HTTPError(request.full_url, status, "private reason", {}, io.BytesIO(b"x" * (BODY_LIMIT + 1)))
    monkeypatch.setattr("stoke.httpget.build_opener", lambda handler: Opener())
    with pytest.raises(HttpGetError, match="^response too large$"):
        asyncio.run(http_get(CLAUDE_URL, {}, 1))


@pytest.mark.parametrize("error", [URLError("private query secret"), OSError("private body secret"), TimeoutError("private token secret"), IncompleteRead(b"")])
def test_http_get_generic_errors(error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> None:
            raise error
    monkeypatch.setattr("stoke.httpget.build_opener", lambda handler: Opener())
    with pytest.raises(HttpGetError) as caught:
        asyncio.run(http_get(CLAUDE_URL, {"Authorization": TOKEN}, 1))
    assert str(caught.value) == ("timed out" if isinstance(error, TimeoutError) else "network error")


def test_version_source_and_unreadable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import tomllib
    stoke_version.cache_clear()
    expected = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["version"]
    assert stoke_version() == expected
    def unavailable(*args: Any, **kwargs: Any) -> str:
        raise OSError("private error")
    monkeypatch.setattr(Path, "read_text", unavailable)
    assert stoke_version() == expected
    stoke_version.cache_clear()
    try:
        assert stoke_version() is None
    finally:
        stoke_version.cache_clear()
