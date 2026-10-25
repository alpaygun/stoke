"""Enabled tool registry; dry run replaces every real provider."""

from typing import Protocol

from ..config import Settings
from ..runner import Runner, run_command
from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .dryrun import DryRunProvider, DryRunReader


class ProviderFactory(Protocol):
    """A provider constructor with capabilities available before instantiation."""

    manual_resets: bool
    reset_closes_window: bool
    auth_labels: dict[str, dict[str, str]]

    def __call__(self, settings: Settings, runner: Runner) -> Provider: ...


FACTORIES: dict[str, tuple[str, ProviderFactory]] = {
    "claude": ("Claude", ClaudeProvider),
    "codex": ("Codex", CodexProvider),
}


def create_providers(settings: Settings, runner: Runner = run_command) -> dict[str, Provider]:
    """Build providers in the configured display order."""
    providers: dict[str, Provider] = {}
    for name in settings.tools:
        label, factory = FACTORIES[name]
        dry_factory = DryRunReader if hasattr(factory, "read_limits") else DryRunProvider
        providers[name] = (
            dry_factory(name, label, settings, manual_resets=factory.manual_resets,
                        reset_closes_window=factory.reset_closes_window, auth_labels=factory.auth_labels)
            if settings.dry_run else factory(settings, runner)
        )
    return providers
