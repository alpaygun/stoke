"""Enabled tool registry; dry run replaces every real provider."""

from typing import Callable

from ..config import Settings
from ..runner import Runner, run_command
from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .dryrun import DryRunProvider

FACTORIES: dict[str, tuple[str, Callable[[Settings, Runner], Provider]]] = {
    "claude": ("Claude", ClaudeProvider),
    "codex": ("Codex", CodexProvider),
}


def create_providers(settings: Settings, runner: Runner = run_command) -> dict[str, Provider]:
    """Build providers in the configured display order."""
    providers: dict[str, Provider] = {}
    for name in settings.tools:
        label, factory = FACTORIES[name]
        providers[name] = (
            DryRunProvider(name, label, settings) if settings.dry_run else factory(settings, runner)
        )
    return providers
