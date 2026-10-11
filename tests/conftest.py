"""Shared test settings and providers; no test may execute a real command."""

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from stoke.config import Settings, load_settings
from stoke.models import LimitsSnapshot, ResetCredit, ResetCredits, StokeResult, Window


class FakeProvider:
    """Deterministic provider with event-controlled blocking for concurrency tests."""

    def __init__(self, name: str = "claude", result: StokeResult | Exception | None = None) -> None:
        self.name = name
        self.label = {"claude": "Claude", "codex": "Codex"}[name]
        self.result = result if result is not None else StokeResult(Window(2.0, 19000, 300), None, 12)
        self.calls = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def stoke(self) -> StokeResult:
        """Signal invocation, optionally block, then return or fail."""
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeReader(FakeProvider):
    """A provider whose independent read can be controlled without I/O."""

    def __init__(self) -> None:
        super().__init__("codex")
        self.read_result: LimitsSnapshot | Exception = LimitsSnapshot(
            Window(8, 99999, 301), Window(29, 600000, 10080),
            ResetCredits(1, [ResetCredit("Full reset", 10, 700000)], 0),
        )
        self.read_calls = 0
        self.read_started = asyncio.Event()
        self.read_release: asyncio.Event | None = None

    async def read_limits(self) -> LimitsSnapshot:
        self.read_calls += 1
        self.read_started.set()
        if self.read_release is not None:
            await self.read_release.wait()
        if isinstance(self.read_result, Exception):
            raise self.read_result
        return self.read_result


@pytest.fixture(autouse=True)
def forbid_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately if any test accidentally reaches a real subprocess."""
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Tests must inject a runner; real subprocesses are forbidden")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    """Build isolated settings without reading the host environment."""
    def create(**overrides: str) -> Settings:
        return load_settings({
            "STOKE_DATA_DIR": str(tmp_path / "data"),
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "STOKE_AUTO_DEFAULT": "0",
            **overrides,
        })
    return create


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """Read an explicitly named CLI fixture."""
    def read(name: str) -> str:
        return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")
    return read
