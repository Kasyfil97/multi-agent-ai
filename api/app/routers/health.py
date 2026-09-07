"""Health (liveness) and readiness (dependency) endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response, status

from .. import __version__
from ..schemas import HealthResponse, ReadyResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    started = request.app.state.started_at
    return HealthResponse(status="ok", version=__version__,
                          uptime_s=round(time.time() - started, 1))


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request, response: Response):
    resources = getattr(request.app.state, "resources", None)
    if resources is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="unavailable", checks={})
    # readiness touches network/DB → run off the event loop
    import anyio
    rep = await anyio.to_thread.run_sync(resources.readiness)
    if rep["status"] == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(**rep)
