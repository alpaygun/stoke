"""Configuration defaults, strict validation and secret-safe representation."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from stoke.config import load_settings


def test_defaults_and_frozen_environment_snapshot() -> None:
    source = {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-secret"}
    settings = load_settings(source)
    assert settings.dry_run is True
    assert settings.tools == ("claude", "codex")
    assert settings.auto_default is True
    assert settings.delay_seconds == 90
    assert settings.retry_seconds == (60, 300, 900, 1800, 3600)
    assert settings.timeout_seconds == 180
    assert settings.claude_reader_dir == Path("/data/claude-reader")
    assert settings.claude_token_expires is None
    assert settings.data_dir == Path("/data")
    assert settings.host == "0.0.0.0" and settings.port == 8787
    assert settings.dry_run_window_seconds == 18000 and settings.dry_run_fail == ()
    assert settings.codex_home == Path("/home/stoke/.codex")
    assert settings.timezone.key == "UTC"
    assert "synthetic-secret" not in repr(settings)
    source["CLAUDE_CODE_OAUTH_TOKEN"] = "changed"
    assert settings.process_env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-secret"
    with pytest.raises(FrozenInstanceError):
        settings.port = 1  # type: ignore[misc]


@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("no", False), ("1", True),
    ("False", True), (" 0", True), ("garbage", True), ("", True),
])
def test_dry_run_is_disabled_only_by_exact_values(value: str, expected: bool) -> None:
    assert load_settings({"STOKE_DRY_RUN": value}).dry_run is expected


@pytest.mark.parametrize("key,value", [
    ("STOKE_TOOLS", ""), ("STOKE_TOOLS", "other"), ("STOKE_TOOLS", "claude,claude"),
    ("STOKE_AUTO_DEFAULT", "maybe"), ("STOKE_DELAY_SECONDS", "-1"),
    ("STOKE_DELAY_SECONDS", "1.5"), ("STOKE_RETRY_SECONDS", ""),
    ("STOKE_RETRY_SECONDS", "60,0"), ("STOKE_RETRY_SECONDS", "a"),
    ("STOKE_TIMEOUT_SECONDS", "0"), ("STOKE_TIMEOUT_SECONDS", "nan"),
    ("STOKE_TIMEOUT_SECONDS", "inf"), ("STOKE_TIMEOUT_SECONDS", "a"),
    ("STOKE_PORT", "0"), ("STOKE_PORT", "65536"), ("STOKE_HOST", " "),
    ("STOKE_DRY_RUN_WINDOW_SECONDS", "0"), ("STOKE_DRY_RUN_FAIL", "other"),
    ("STOKE_DATA_DIR", ""), ("CODEX_HOME", ""),
    ("STOKE_CLAUDE_READER_DIR", ""),
])
def test_invalid_configuration_names_the_variable(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key):
        load_settings({key: value})


def test_overrides_and_display_order() -> None:
    settings = load_settings({
        "STOKE_TOOLS": "codex,claude", "STOKE_AUTO_DEFAULT": "no", "STOKE_DELAY_SECONDS": "0",
        "STOKE_RETRY_SECONDS": "1,2", "STOKE_TIMEOUT_SECONDS": "0.5", "STOKE_HOST": "127.0.0.1",
        "STOKE_PORT": "9000", "STOKE_DRY_RUN_WINDOW_SECONDS": "30", "STOKE_DRY_RUN_FAIL": "codex",
        "STOKE_DATA_DIR": "data", "CODEX_HOME": "codex-home",
    })
    assert settings.tools == ("codex", "claude")
    assert not settings.auto_default and settings.delay_seconds == 0
    assert settings.retry_seconds == (1, 2) and settings.timeout_seconds == 0.5
    assert settings.host == "127.0.0.1" and settings.port == 9000
    assert settings.dry_run_window_seconds == 30 and settings.dry_run_fail == ("codex",)
    assert settings.data_dir == Path("data") and settings.codex_home == Path("codex-home")


@pytest.mark.parametrize("key", ["Unknown/Zone", "", "../Europe/Istanbul", "/Europe/Istanbul"])
def test_invalid_timezone(key: str) -> None:
    with pytest.raises(ValueError, match="^STOKE_TIMEZONE must be a valid IANA time zone$"):
        load_settings({"STOKE_TIMEZONE": key})


def test_timezone_override() -> None:
    assert load_settings({"STOKE_TIMEZONE": "America/New_York"}).timezone.key == "America/New_York"
