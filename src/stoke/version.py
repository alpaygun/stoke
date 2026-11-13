"""Application version from the same source used for releases."""

import tomllib
from functools import cache
from pathlib import Path


@cache
def stoke_version() -> str | None:
    """Read once; source checkouts and images share the same project root."""
    try:
        value = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
        version = value.get("project", {}).get("version")
        return version if isinstance(version, str) and version else None
    except (OSError, UnicodeError, ValueError, AttributeError):
        return None
