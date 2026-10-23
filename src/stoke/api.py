"""FastAPI JSON endpoints and the status page."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import clock
from .config import Settings
from .models import ToolState, serialize_tool
from .providers.base import ProviderError
from .scheduler import AlreadyRunning, ManualResetsUnsupported, PlanConflict, RefreshUnsupported, Scheduler

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

    def serialize(state: ToolState, now: int | None = None) -> dict[str, Any]:
        return serialize_tool(
            state, clock.now() if now is None else now,
            refreshable=scheduler.can_refresh(state.name), settings=settings,
            manual_resets_allowed=scheduler.providers[state.name].manual_resets,
            auth_labels=scheduler.providers[state.name].auth_labels,
        )

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        now = clock.now()
        return {"dry_run": settings.dry_run, "server_time": now, "timezone": settings.timezone.key, "tools": {
            name: serialize(state, now)
            for name, state in scheduler.states.items()
        }}

    @app.post("/api/tools/{name}/stoke")
    async def manual_stoke(name: str, request: Request) -> Any:
        require_tool(name)
        break_plan = False
        if await request.body():
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(422, "body must contain break_plan: bool") from None
            if not isinstance(body, dict) or set(body) - {"break_plan"} or type(body.get("break_plan", False)) is not bool:
                raise HTTPException(422, "body must contain break_plan: bool")
            break_plan = body.get("break_plan", False)
        try:
            state = await scheduler.stoke_now(name, "manual", break_plan=break_plan)
        except AlreadyRunning:
            raise HTTPException(409, "stoke already running") from None
        except PlanConflict as conflict:
            return JSONResponse({
                "detail": "stoke would break the plan", "code": "plan_conflict",
                "plan_target_at": conflict.target_at,
            }, status_code=409)
        return serialize(state)

    @app.post("/api/tools/{name}/plan")
    async def set_plan(name: str, request: Request) -> dict[str, Any]:
        require_tool(name)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"time", "repeat", "auto_after"}:
                raise ValueError("body must contain time, repeat and auto_after")
            state = scheduler.set_plan(name, body["time"], body["repeat"], body["auto_after"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return serialize(state)

    @app.post("/api/tools/{name}/plan/cancel")
    async def cancel_plan(name: str) -> dict[str, Any]:
        require_tool(name)
        return serialize(scheduler.cancel_plan(name))

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
        return serialize(state)

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
        return serialize(state)

    @app.post("/api/tools/{name}/weekly-resets/dismiss")
    async def dismiss_weekly_reset(name: str, request: Request) -> dict[str, Any]:
        require_tool(name)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str):
                raise ValueError("body must contain exactly id: str")
            state = scheduler.dismiss_weekly_reset(name, body["id"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return serialize(state)

    @app.post("/api/tools/{name}/auto-refresh")
    async def set_auto_refresh(name: str, request: Request) -> dict[str, Any]:
        require_tool(name)
        if not scheduler.can_refresh(name):
            raise HTTPException(404, "refresh not supported")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"enabled", "seconds"}:
                raise ValueError("body must contain exactly enabled: bool and seconds: int")
            state = scheduler.set_auto_refresh(name, body["enabled"], body["seconds"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return serialize(state)

    @app.post("/api/tools/{name}/resets")
    async def add_reset(name: str, request: Request) -> dict[str, Any]:
        return await change_reset(name, request, remove=False)

    @app.post("/api/tools/{name}/resets/remove")
    async def remove_reset(name: str, request: Request) -> dict[str, Any]:
        return await change_reset(name, request, remove=True)

    async def change_reset(name: str, request: Request, *, remove: bool) -> dict[str, Any]:
        require_tool(name)
        if not scheduler.providers[name].manual_resets:
            raise HTTPException(404, "manual resets not supported")
        try:
            body = await request.json()
            keys = {"id"} if remove else {"title", "expires_on"}
            if not isinstance(body, dict) or set(body) != keys or any(not isinstance(value, str) for value in body.values()):
                raise ValueError("body must contain exactly " + ("id: str" if remove else "title: str and expires_on: YYYY-MM-DD"))
            state = scheduler.remove_manual_reset(name, body["id"]) if remove else scheduler.add_manual_reset(name, body["title"], body["expires_on"])
        except ManualResetsUnsupported:
            raise HTTPException(404, "manual resets not supported") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return serialize(state)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_PAGE)

    return app
