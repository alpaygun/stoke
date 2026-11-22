"""Antigravity quota parsing, CLI isolation, and cleanup tests."""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Sequence

import pytest

from stoke.config import Settings
from stoke.models import QuotaGroup, ScopedLimit, Window
from stoke.providers.antigravity import AntigravityProvider, parse_usage
from stoke.providers.base import LimitsUnreadable, ProviderError, ReaderSignedOut, StokeNotDelivered, StokeSignedOut, StokeUnverified, redact
from stoke.runner import CommandResult

NOW = 1800000000
HOUR = 3600
WEEK = 7 * 24 * HOUR
CONVERSATION = "00000000-0000-4000-8000-000000000001"
SIBLING = "00000000-0000-4000-8000-000000000002"
PRIVATE = "private-output-sentinel"
AUTH_ERROR = "authentication failed or timed out"


def bucket(key: str, fraction: float = 0.75, end: int | None = None, **overrides: Any) -> dict[str, Any]:
    five_hour = key.endswith("-5h")
    if end is None:
        end = NOW + (4 * HOUR if five_hour else WEEK - HOUR)
    return {
        "id": key, "remaining_fraction": fraction,
        "reset_time": datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z"),
        "window": "5h" if five_hour else "weekly", **overrides,
    }


def usage(*groups: list[dict[str, Any]], turns: int = 0) -> str:
    return json.dumps({
        "status": "SUCCESS", "num_turns": turns, "response": PRIVATE,
        "command": {"name": "usage", "data": {"groups": [
            {"name": "Unused label", "buckets": entries} for entries in groups
        ]}},
    })


def read_result(*, confirmed: bool = True) -> CommandResult:
    fraction = 0.875 if confirmed else 1
    return CommandResult(0, usage(
        [bucket("gemini-5h", fraction), bucket("gemini-weekly", 0.75)],
        [bucket("3p-5h", 0.5), bucket("gemini-argon-wtu-weekly", 0.9375)],
    ), PRIVATE, 19, False)


def prompt_result(
    status: object = "SUCCESS", *, error: str = PRIVATE, conversation_id: object = CONVERSATION,
    timeout: bool = False,
) -> CommandResult:
    return CommandResult(
        0 if status == "SUCCESS" else 1,
        json.dumps({
            "status": status, "conversation_id": conversation_id, "response": PRIVATE,
            "error": error, "finished_reason": PRIVATE,
        }), PRIVATE, 37, timeout,
    )


def bad_read() -> CommandResult:
    return CommandResult(1, PRIVATE, PRIVATE, 3, False)


def test_usage_full_shape_matches_ids_and_handles_json_lines() -> None:
    sample = usage(
        [bucket("gemini-weekly", 0.875), bucket("3p-5h", 0.75)],
        [bucket("gemini-argon-wtu-weekly", 0.9375)],
        [bucket("3p-weekly", 0.5), bucket("gemini-5h", 0.625)],
    )
    parsed = parse_usage("  " + sample + "\n", NOW)
    assert parse_usage('noise\n{"status":"ERROR"}\n' + sample + "\n[]", NOW) == parsed
    assert parsed.five_hour == Window(37.5, NOW + 4 * HOUR, 300)
    assert parsed.weekly == Window(12.5, NOW + WEEK - HOUR, 10080)
    assert parsed.scoped == [ScopedLimit("Argon", 6.25, NOW + WEEK - HOUR)]
    assert parsed.groups == [
        QuotaGroup("third_party", Window(25, NOW + 4 * HOUR, 300), Window(50, NOW + WEEK - HOUR, 10080)),
    ]
    assert parsed.observed_at == NOW
    assert parsed.resets is parsed.plan_name is None
    assert parsed.window_confirmed and not parsed.weekly_synthetic


@pytest.mark.parametrize("overrides", [
    {"remaining_fraction": True}, {"remaining_fraction": float("nan")},
    {"remaining_fraction": float("inf")}, {"remaining_fraction": 1.01},
    {"remaining_fraction": -0.01}, {"remaining_fraction": "0.5"},
    {"reset_time": "invalid"}, {"reset_time": "2027-01-01T00:00:00"},
    {"window": "weekly"},
])
def test_usage_rejects_invalid_required_bucket(overrides: dict[str, Any]) -> None:
    with pytest.raises(LimitsUnreadable, match="^limits unreadable$"):
        parse_usage(usage([bucket("gemini-5h", **overrides)]), NOW)


@pytest.mark.parametrize("text", [
    "noise", "[]", "null", '{"status":"SUCCESS"}',
    '{"status":"SUCCESS","command":{"data":{"groups":{}}}}',
    usage([bucket("gemini-weekly")]),
    usage([bucket("gemini-5h")], [bucket("gemini-5h")]),
    usage([bucket("gemini-5h"), bucket("unknown")], [bucket("unknown")]),
])
def test_usage_requires_object_groups_unique_ids_and_gemini(text: str) -> None:
    with pytest.raises(LimitsUnreadable, match="^limits unreadable$"):
        parse_usage(text, NOW)


def test_usage_omits_missing_or_invalid_optional_buckets(caplog: pytest.LogCaptureFixture) -> None:
    five = bucket("gemini-5h")
    identifiers = ("gemini-weekly", "3p-5h", "3p-weekly", "gemini-argon-wtu-weekly")
    absent = parse_usage(usage([five]), NOW)
    assert absent.weekly is None and not absent.weekly_synthetic
    assert absent.scoped == absent.groups == []
    assert not caplog.records
    assert parse_usage(usage([five] + [bucket(key, reset_time=PRIVATE) for key in identifiers]), NOW) == absent
    assert [record.getMessage() for record in caplog.records] == [
        f"tool=antigravity invalid usage bucket id={key}"
        for key in ("gemini-weekly", "gemini-argon-wtu-weekly", "3p-5h", "3p-weekly")
    ]
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("offset,synthetic", [(-121, False), (-120, True), (0, True), (120, True), (121, False)])
def test_usage_synthetic_windows(offset: int, synthetic: bool) -> None:
    day_end, week_end = NOW + 5 * HOUR + offset, NOW + WEEK + offset
    parsed = parse_usage(usage([
        bucket("gemini-5h", 1, day_end), bucket("gemini-weekly", 1, week_end),
        bucket("gemini-argon-wtu-weekly", 1, week_end), bucket("3p-weekly", 1, week_end),
        bucket("3p-5h", 1, day_end),
    ]), NOW)
    assert parsed.five_hour == Window(0, day_end, 300) and not parsed.window_confirmed
    assert parsed.weekly == Window(0, week_end, 10080) and parsed.weekly_synthetic is synthetic
    assert parsed.scoped == [ScopedLimit("Argon", 0, None if synthetic else week_end)]
    assert parsed.groups == [QuotaGroup(
        "third_party", None if synthetic else Window(0, day_end, 300),
        None if synthetic else Window(0, week_end, 10080), synthetic, synthetic,
    )]


@pytest.mark.parametrize("fraction,end,confirmed", [
    (1.0, NOW + 4 * HOUR, False), (0.99, NOW + 5 * HOUR, True),
    (0, NOW + 1, True), (0.99, NOW, False), (0.99, NOW - 1, False),
])
def test_usage_only_confirms_nonempty_future_window(fraction: float, end: int, confirmed: bool) -> None:
    assert parse_usage(usage([bucket("gemini-5h", fraction, end)]), NOW).window_confirmed is confirmed


@pytest.mark.parametrize("status,error,expected_type,message", [
    ("ERROR", AUTH_ERROR, ReaderSignedOut, "Antigravity signed out"),
    ("ERROR", PRIVATE, ProviderError, "Antigravity usage read failed"),
    ("UNKNOWN", AUTH_ERROR, ProviderError, "Antigravity usage read failed"),
    (None, None, ProviderError, "Antigravity usage read failed"),
])
def test_usage_status_errors_are_safe(
    status: str | None, error: str | None, expected_type: type[ProviderError], message: str,
) -> None:
    with pytest.raises(expected_type) as raised:
        parse_usage(json.dumps({"status": status, "error": error, "response": PRIVATE}), NOW)
    assert type(raised.value) is expected_type and str(raised.value) == message


def test_usage_warns_once_if_model_turns_reported(caplog: pytest.LogCaptureFixture) -> None:
    assert parse_usage(usage([bucket("gemini-5h")], turns=2), NOW).window_confirmed
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("WARNING", "tool=antigravity usage read reported model turns"),
    ]


class Call(NamedTuple):
    argv: Sequence[str]
    cwd: Path
    timeout: float
    env: Mapping[str, str] | None
    stdin: bytes | None
    log_file: Path


class FakeRunner:
    def __init__(self, *outcomes: CommandResult | OSError) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[Call] = []

    async def __call__(
        self, argv: Sequence[str], cwd: Path, timeout_s: float,
        env: Mapping[str, str] | None = None, *, stdin: bytes | None = None,
    ) -> CommandResult:
        log_file = Path(argv[argv.index("--log-file") + 1])
        assert cwd.is_dir() and log_file.parent.is_dir()
        log_file.write_text(PRIVATE, encoding="utf-8")
        self.calls.append(Call(argv, cwd, timeout_s, env, stdin, log_file))
        outcome = next(self.outcomes)
        if isinstance(outcome, OSError):
            raise outcome
        return outcome


@pytest.fixture
def settings(settings_factory: Callable[..., Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr("stoke.clock.now", lambda: NOW)
    return settings_factory(
        STOKE_TOOLS="antigravity", STOKE_DRY_RUN="0", HOME=str(tmp_path / "home"),
        STOKE_TIMEOUT_SECONDS="42",
    )


def token_path(settings: Settings) -> Path:
    return settings.antigravity_dir / "antigravity-oauth-token"


@pytest.fixture
def signed_in(settings: Settings) -> Path:
    token = token_path(settings)
    token.parent.mkdir(parents=True)
    token.write_text("synthetic credential placeholder", encoding="utf-8")
    token.chmod(0o600)
    return token


@pytest.mark.parametrize("kind", ["missing", "empty", "directory"])
def test_token_preflight_skips_cli(settings: Settings, kind: str) -> None:
    token = token_path(settings)
    token.parent.mkdir(parents=True)
    if kind == "empty":
        token.touch()
    elif kind == "directory":
        token.mkdir()
    runner = FakeRunner()
    provider = AntigravityProvider(settings, runner)
    with pytest.raises(ReaderSignedOut, match="^Antigravity signed out$"):
        asyncio.run(provider.read_limits())
    with pytest.raises(StokeSignedOut, match="^Antigravity signed out$"):
        asyncio.run(provider.stoke())
    assert not runner.calls
    assert not (settings.data_dir / "workdir-antigravity").exists()


def test_stoke_cli_arguments_environment_isolation_and_duration(
    settings_factory: Callable[..., Settings], signed_in: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = (
        "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    )
    allowed = {key: f"fixture-{key}" for key in keys}
    allowed["HOME"] = str(signed_in.parents[2])
    settings = settings_factory(
        **allowed, STOKE_TIMEOUT_SECONDS="42", CLAUDE_CODE_OAUTH_TOKEN=PRIVATE,
        AGY_CLI_DISABLE_AUTO_UPDATE="1", OTHER=PRIVATE,
    )
    original = signed_in.read_bytes()
    original_open = Path.open

    def guard_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path != signed_in, "The provider must only stat the login file"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_open)
    runner = FakeRunner(prompt_result(), read_result())
    result = asyncio.run(AntigravityProvider(settings, runner).stoke())
    with original_open(signed_in, "rb") as token:
        assert token.read() == original
    prompt, read = runner.calls
    assert prompt.argv == (
        "agy", "--model", "gemini-3.8-flash-low", "--output-format", "json", "--disable-slash-commands",
        "--log-file", str(prompt.log_file), "-p", "hi",
    )
    assert read.argv == ("agy", "--output-format", "json", "--log-file", str(read.log_file), "-p", "/usage")
    assert prompt.timeout == 42 and read.timeout == 75
    assert prompt.log_file.parent != read.log_file.parent
    for call in runner.calls:
        assert call.cwd == settings.data_dir / "workdir-antigravity"
        assert call.stdin == b""
        assert call.env == {**allowed, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
        assert call.log_file.parent.name.startswith("stoke-agy-")
        assert not call.log_file.parent.exists()
    assert not (settings.data_dir / "workdir").exists()
    assert result.duration_ms == 37
    assert result.five_hour == Window(12.5, NOW + 4 * HOUR, 300)
    assert result.weekly == Window(25, NOW + WEEK - HOUR, 10080)
    assert result.scoped == [ScopedLimit("Argon", 6.25, NOW + WEEK - HOUR)]
    assert result.groups == [QuotaGroup("third_party", Window(50, NOW + 4 * HOUR, 300), None)]


@pytest.mark.parametrize("timeout,expected", [("77.5", 77.5), ("180", 90)])
def test_read_timeout_and_cli_mode(
    timeout: str, expected: float, settings_factory: Callable[..., Settings], signed_in: Path,
) -> None:
    settings = settings_factory(HOME=str(signed_in.parents[2]), STOKE_TIMEOUT_SECONDS=timeout)
    runner = FakeRunner(read_result())
    assert asyncio.run(AntigravityProvider(settings, runner).read_limits("cli")).window_confirmed
    assert len(runner.calls) == 1 and runner.calls[0].timeout == expected


@pytest.mark.parametrize("result,expected_type,message", [
    (OSError(PRIVATE), ProviderError, "Antigravity CLI could not be started"),
    (CommandResult(-1, PRIVATE, PRIVATE, 1, True), ProviderError, "Antigravity CLI timed out"),
    (CommandResult(1, '{"status":"ERROR","error":"authentication failed or timed out"}', PRIVATE, 1, False),
     ReaderSignedOut, "Antigravity signed out"),
    (bad_read(), ProviderError, "Antigravity usage read failed"),
    (CommandResult(0, PRIVATE, PRIVATE, 1, False), LimitsUnreadable, "limits unreadable"),
])
def test_read_failure_never_exposes_output(
    result: CommandResult | OSError, expected_type: type[ProviderError], message: str,
    settings: Settings, signed_in: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner(result)
    with pytest.raises(expected_type) as raised:
        asyncio.run(AntigravityProvider(settings, runner).read_limits())
    assert type(raised.value) is expected_type and str(raised.value) == message
    assert not runner.calls[0].log_file.parent.exists()
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("result,expected_type,message", [
    (OSError(PRIVATE), ProviderError, "Antigravity CLI could not be started"),
    (prompt_result("ERROR", error=AUTH_ERROR), StokeSignedOut, "Antigravity signed out"),
    (prompt_result("ERROR"), ProviderError, "Antigravity prompt failed"),
])
def test_definite_prompt_failure_has_no_usage_read(
    result: CommandResult | OSError, expected_type: type[ProviderError], message: str, settings: Settings, signed_in: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner(result)
    with pytest.raises(expected_type) as raised:
        asyncio.run(AntigravityProvider(settings, runner).stoke())
    assert type(raised.value) is expected_type and str(raised.value) == message
    assert len(runner.calls) == 1 and not runner.calls[0].log_file.parent.exists()
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("prompt", [
    prompt_result(), prompt_result(timeout=True), prompt_result("UNKNOWN"),
    CommandResult(1, PRIVATE, PRIVATE, 37, False),
], ids=["delivered", "timeout", "unknown-status", "unparseable"])
@pytest.mark.parametrize("usage_outcome", ["confirmed", "unconfirmed", "failed"])
def test_prompt_reconciliation_mapping(
    prompt: CommandResult, usage_outcome: str, settings: Settings, signed_in: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delivered = prompt == prompt_result()
    usage_command = bad_read() if usage_outcome == "failed" else read_result(confirmed=usage_outcome == "confirmed")
    runner = FakeRunner(prompt, usage_command)
    provider = AntigravityProvider(settings, runner)
    if usage_outcome == "confirmed":
        assert asyncio.run(provider.stoke()).duration_ms == prompt.duration_ms
    elif usage_outcome == "failed" or delivered:
        with pytest.raises(StokeUnverified, match="^prompt sent; usage unverified$") as raised:
            asyncio.run(provider.stoke())
        assert raised.value.delivered is delivered
    else:
        with pytest.raises(StokeNotDelivered, match="^Antigravity prompt not delivered$") as raised:
            asyncio.run(provider.stoke())
        assert raised.value.snapshot.groups
    assert [call.argv[-1] for call in runner.calls] == ["hi", "/usage"]
    assert all(not call.log_file.parent.exists() for call in runner.calls)
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("outcome", ["confirmed", "unconfirmed", "failed", "expired_during_read"])
def test_verify_only_reads_and_preserves_delivery_certainty(
    delivered: bool, outcome: str, settings: Settings, signed_in: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if outcome == "expired_during_read":
        ticks = iter([NOW, NOW + 4 * HOUR])
        monkeypatch.setattr("stoke.clock.now", lambda: next(ticks))
    runner = FakeRunner(bad_read() if outcome == "failed" else read_result(confirmed=outcome != "unconfirmed"))
    provider = AntigravityProvider(settings, runner)
    if outcome == "confirmed":
        result = asyncio.run(provider.verify_stoke(delivered))
        assert result.duration_ms == 19 and result.groups and result.scoped
    elif delivered or outcome == "failed":
        with pytest.raises(StokeUnverified) as raised:
            asyncio.run(provider.verify_stoke(delivered))
        assert raised.value.delivered is delivered
    else:
        with pytest.raises(StokeNotDelivered, match="^Antigravity prompt not delivered$") as raised:
            asyncio.run(provider.verify_stoke(delivered))
        assert raised.value.snapshot.groups
    assert [call.argv[-1] for call in runner.calls] == ["/usage"]


def write_files(root: Path, names: Sequence[str]) -> list[Path]:
    files = [root / name for name in names]
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("synthetic artifact", encoding="utf-8")
    return files


def conversation_files(root: Path, conversation_id: str) -> list[Path]:
    db = f"conversations/{conversation_id}.db"
    return write_files(root, [
        db, db + "-wal", db + "-shm", db + "-journal",
        f"brain/{conversation_id}/nested/artifact", f"presence/{conversation_id}.lock",
    ])


@pytest.mark.parametrize("status,timeout", [("SUCCESS", False), ("ERROR", False), ("UNKNOWN", True)])
def test_cleanup_only_removes_prompt_artifacts_even_on_failure_or_timeout(
    status: str, timeout: bool, settings: Settings, signed_in: Path,
) -> None:
    root = settings.antigravity_dir
    files = conversation_files(root, CONVERSATION)
    sibling = conversation_files(root, SIBLING)
    shared = write_files(root, [
        "conversation_summaries.db", "jetbox_summaries_proto.pb", "cache/example",
        "history.jsonl", "conversation_metadata.json", f"conversations/{CONVERSATION}.db-other",
    ])
    runner = FakeRunner(prompt_result(status, timeout=timeout), read_result())
    provider = AntigravityProvider(settings, runner)
    if status == "ERROR":
        with pytest.raises(ProviderError, match="Antigravity prompt failed"):
            asyncio.run(provider.stoke())
    else:
        asyncio.run(provider.stoke())
    assert not any(file.exists() for file in files)
    assert not (root / "brain" / CONVERSATION).exists()
    assert all(file.exists() for file in [signed_in, *sibling, *shared])


@pytest.mark.parametrize("conversation_id", [None, "", "../../invalid", CONVERSATION + "\n"])
@pytest.mark.parametrize("delivered", [True, False])
def test_cleanup_skips_bad_ids_and_warns_only_for_delivery(
    conversation_id: object, delivered: bool, settings: Settings, signed_in: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    files = conversation_files(settings.antigravity_dir, CONVERSATION)
    runner = FakeRunner(prompt_result("SUCCESS" if delivered else "UNKNOWN", conversation_id=conversation_id), read_result())
    asyncio.run(AntigravityProvider(settings, runner).stoke())
    assert all(file.exists() for file in files)
    messages = [record.getMessage() for record in caplog.records]
    assert messages == (["antigravity conversation cleanup skipped: missing or invalid id"] if delivered else [])


def test_cleanup_unlinks_brain_symlink_and_does_not_follow_symlinked_parent(
    settings: Settings, signed_in: Path, tmp_path: Path,
) -> None:
    root = settings.antigravity_dir
    outside = tmp_path / "outside"
    preserved = write_files(outside, ["artifact", f"{CONVERSATION}.db"])
    brain = root / "brain" / CONVERSATION
    brain.parent.mkdir()
    try:
        brain.symlink_to(outside, target_is_directory=True)
        (root / "conversations").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        pytest.skip("Symlink creation requires privileges on this system")
    runner = FakeRunner(prompt_result(), read_result())
    asyncio.run(AntigravityProvider(settings, runner).stoke())
    assert not brain.is_symlink()
    assert (root / "conversations").is_symlink()
    assert all(file.exists() for file in preserved)


def test_cleanup_error_does_not_fail_stoke_or_stop_other_deletions(
    settings: Settings, signed_in: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    db, *remaining = conversation_files(settings.antigravity_dir, CONVERSATION)
    unlink = Path.unlink

    def blocked(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == db:
            raise PermissionError(PRIVATE)
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked)
    runner = FakeRunner(prompt_result(), read_result())
    assert asyncio.run(AntigravityProvider(settings, runner).stoke()).five_hour.used_percent == 12.5
    assert db.exists() and not any(file.exists() for file in remaining)
    assert [record.getMessage() for record in caplog.records] == [
        "antigravity conversation cleanup failed: PermissionError",
    ]


def test_third_party_prompt_uses_fixed_model_and_shares_the_full_snapshot(
    settings: Settings, signed_in: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    conversation = signed_in.parent / "conversations" / f"{CONVERSATION}.db"
    conversation.parent.mkdir()
    conversation.write_text(PRIVATE, encoding="utf-8")
    runner = FakeRunner(prompt_result(), read_result(confirmed=False), read_result())
    provider = AntigravityProvider(settings, runner)
    result = asyncio.run(provider.stoke_third_party())
    verified = asyncio.run(provider.verify_third_party(True))
    prompt, read, verify = runner.calls
    assert prompt.argv == (
        "agy", "--model", "gpt-oss-120b-medium", "--output-format", "json", "--disable-slash-commands",
        "--log-file", str(prompt.log_file), "-p", "hi",
    )
    for call in (read, verify):
        assert call.argv == ("agy", "--output-format", "json", "--log-file", str(call.log_file), "-p", "/usage")
    assert result.window == verified.window == Window(50, NOW + 4 * HOUR, 300)
    assert not result.snapshot.window_confirmed and verified.snapshot.window_confirmed
    assert (result.duration_ms, verified.duration_ms) == (37, 19)
    assert not conversation.exists()
    assert PRIVATE not in caplog.text and PRIVATE not in repr(result)


@pytest.mark.parametrize("prompt", [prompt_result(), prompt_result("ERROR"), prompt_result(timeout=True)])
@pytest.mark.parametrize("buckets", [[], [bucket("3p-weekly"), bucket("3p-5h", reset_time=PRIVATE)]])
def test_missing_3p_usage_after_any_prompt_requires_verification(
    settings: Settings, signed_in: Path, prompt: CommandResult, buckets: list[dict[str, Any]],
) -> None:
    read = CommandResult(0, usage([bucket("gemini-5h")], buckets), PRIVATE, 19, False)
    runner = FakeRunner(prompt, read)
    with pytest.raises(StokeUnverified, match="^prompt sent; usage unverified$") as raised:
        asyncio.run(AntigravityProvider(settings, runner).stoke_third_party())
    assert raised.value.delivered and raised.value.snapshot.window_confirmed
    assert len(runner.calls) == 2 and PRIVATE not in repr(raised.value.snapshot)


@pytest.mark.parametrize("delivered", [True, False])
def test_synthetic_3p_window_does_not_confirm_a_prompt(
    settings: Settings, signed_in: Path, delivered: bool,
) -> None:
    sample = usage([bucket("gemini-5h"), bucket("3p-5h", 1, NOW + 5 * HOUR)])
    runner = FakeRunner(CommandResult(0, sample, PRIVATE, 19, False))
    with pytest.raises(StokeUnverified if delivered else StokeNotDelivered) as raised:
        asyncio.run(AntigravityProvider(settings, runner).verify_third_party(delivered))
    assert raised.value.snapshot.groups == [QuotaGroup("third_party", None, None, five_hour_synthetic=True)]
    assert len(runner.calls) == 1 and runner.calls[0].argv[-1] == "/usage"


def test_3p_verify_keeps_reader_sign_out_distinct(settings: Settings, signed_in: Path) -> None:
    runner = FakeRunner(prompt_result("ERROR", error=AUTH_ERROR))
    with pytest.raises(ReaderSignedOut, match="^Antigravity signed out$"):
        asyncio.run(AntigravityProvider(settings, runner).verify_third_party(True))
    assert len(runner.calls) == 1 and runner.calls[0].argv[-1] == "/usage"


def test_google_oauth_redaction() -> None:
    assert redact("access=ya29.Synthetic-test_0.part refresh=1//Synthetic_test-0 end") == (
        "access=[REDACTED] refresh=[REDACTED] end"
    )
