"""Wire configuration, providers, persistence, scheduling and HTTP in order."""

import logging
import sys

import uvicorn
from fastapi import FastAPI

from .api import create_app
from .config import Settings, load_settings
from .providers import create_providers
from .scheduler import Scheduler
from .store import load


class HealthzAccessFilter(logging.Filter):
    """Keep access logs except health checks, including query strings."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5 or not isinstance(args[2], str):
            return True
        return args[2].split("?", 1)[0] != "/healthz"


def build_app(settings: Settings) -> FastAPI:
    """Build the backend without starting tasks or executing commands."""
    (settings.data_dir / "workdir").mkdir(parents=True, exist_ok=True)
    providers = create_providers(settings)
    path = settings.data_dir / "state.json"
    states = load(path, settings, providers)
    return create_app(Scheduler(providers, states, settings, path), settings)


def run() -> None:
    """Start the service using validated configuration and stdout logging."""
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("uvicorn.access").addFilter(HealthzAccessFilter())
    settings = load_settings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_config=None)
