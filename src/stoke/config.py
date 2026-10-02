"""The only place that reads process environment variables."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    """Validated settings and an unchanged environment snapshot for the CLIs."""

    dry_run: bool
    tools: tuple[str, ...]
    auto_default: bool
    delay_seconds: int
    retry_seconds: tuple[int, ...]
    timeout_seconds: float
    data_dir: Path
    host: str
    port: int
    dry_run_window_seconds: int
    dry_run_fail: tuple[str, ...]
    codex_home: Path
    process_env: Mapping[str, str] = field(repr=False, compare=False)


def _integer(value: str, key: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None
    if result < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    return result


def _boolean(value: str, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{key} must be 1/true/yes or 0/false/no")


def _names(value: str, key: str, allow_empty: bool = False) -> tuple[str, ...]:
    # Import after Settings is defined to keep tool registration in one place.
    from .providers import FACTORIES

    if allow_empty and not value.strip():
        return ()
    names = tuple(part.strip() for part in value.split(","))
    if any(name not in FACTORIES for name in names):
        raise ValueError(f"{key} must contain only registered tools: {', '.join(FACTORIES)}")
    if len(set(names)) != len(names):
        raise ValueError(f"{key} must not contain duplicate tools")
    return names


def _path(value: str, key: str) -> Path:
    if not value.strip() or "\x00" in value:
        raise ValueError(f"{key} must be a nonempty path")
    return Path(value)


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    """Read and validate all configuration without revealing secret values."""
    retry = tuple(
        _integer(part.strip(), "STOKE_RETRY_SECONDS")
        for part in env.get("STOKE_RETRY_SECONDS", "60,300,900,1800,3600").split(",")
    )
    try:
        timeout = float(env.get("STOKE_TIMEOUT_SECONDS", "180"))
    except ValueError:
        raise ValueError("STOKE_TIMEOUT_SECONDS must be a positive number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("STOKE_TIMEOUT_SECONDS must be a positive finite number")
    host = env.get("STOKE_HOST", "0.0.0.0")
    if not host.strip() or any(character.isspace() for character in host):
        raise ValueError("STOKE_HOST must be a nonempty host without whitespace")
    port = _integer(env.get("STOKE_PORT", "8787"), "STOKE_PORT")
    if port > 65535:
        raise ValueError("STOKE_PORT must be at most 65535")
    return Settings(
        dry_run=env.get("STOKE_DRY_RUN", "1") not in {"0", "false", "no"},
        tools=_names(env.get("STOKE_TOOLS", "claude,codex"), "STOKE_TOOLS"),
        auto_default=_boolean(env.get("STOKE_AUTO_DEFAULT", "1"), "STOKE_AUTO_DEFAULT"),
        delay_seconds=_integer(env.get("STOKE_DELAY_SECONDS", "90"), "STOKE_DELAY_SECONDS", 0),
        retry_seconds=retry,
        timeout_seconds=timeout,
        data_dir=_path(env.get("STOKE_DATA_DIR", "/data"), "STOKE_DATA_DIR"),
        host=host,
        port=port,
        dry_run_window_seconds=_integer(
            env.get("STOKE_DRY_RUN_WINDOW_SECONDS", "18000"), "STOKE_DRY_RUN_WINDOW_SECONDS"
        ),
        dry_run_fail=_names(env.get("STOKE_DRY_RUN_FAIL", ""), "STOKE_DRY_RUN_FAIL", True),
        codex_home=_path(env.get("CODEX_HOME", "/home/stoke/.codex"), "CODEX_HOME"),
        process_env=MappingProxyType(dict(env)),
    )
