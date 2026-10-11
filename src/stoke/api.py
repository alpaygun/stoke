"""FastAPI JSON endpoints and the status page."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import clock
from .config import Settings
from .models import serialize_tool
from .providers.base import ProviderError
from .scheduler import AlreadyRunning, RefreshUnsupported, Scheduler

STATIC_PAGE = Path(__file__).resolve().parents[2] / "static" / "index.html"


def create_app(scheduler: Scheduler, settings: Settings) -> FastAPI:
    """Create the app and tie scheduler tasks to its lifespan."""
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def require_header(request: Request, call_next: Any) -> Any:
        if request.method == "POST" and request.headers.get("X-Stoke") != "1":
            return JSONResponse({"detail": "X-Stoke: 1 required"}, status_code=403)
        return await call_next(request)

    def require_tool(name: str) -> None:
        if name not in scheduler.states:
            raise HTTPException(404, "unknown tool")

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        now = clock.now()
        return {"dry_run": settings.dry_run, "server_time": now, "tools": {
            name: serialize_tool(state, now, refreshable=scheduler.can_refresh(name))
            for name, state in scheduler.states.items()
        }}

    @app.post("/api/tools/{name}/stoke")
    async def manual_stoke(name: str) -> dict[str, Any]:
        require_tool(name)
        try:
            state = await scheduler.stoke_now(name, "manual")
        except AlreadyRunning:
            raise HTTPException(409, "stoke already running") from None
        return serialize_tool(state, clock.now(), refreshable=scheduler.can_refresh(name))

    @app.post("/api/tools/{name}/refresh")
    async def refresh_limits(name: str) -> dict[str, Any]:
        require_tool(name)
        try:
            state = await scheduler.refresh_limits(name)
        except RefreshUnsupported:
            raise HTTPException(404, "refresh not supported") from None
        except AlreadyRunning:
            raise HTTPException(409, "already running") from None
        except ProviderError as error:
            raise HTTPException(502, str(error)) from None
        return serialize_tool(state, clock.now(), refreshable=scheduler.can_refresh(name))

    @app.post("/api/tools/{name}/auto")
    async def set_auto(name: str, request: Request) -> dict[str, Any]:
        require_tool(name)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "body must contain enabled: bool") from None
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            raise HTTPException(422, "body must contain enabled: bool")
        state = scheduler.set_auto(name, body["enabled"])
        return serialize_tool(state, clock.now(), refreshable=scheduler.can_refresh(name))

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_PAGE)

    return app
