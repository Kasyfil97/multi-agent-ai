"""FastAPI app factory: lifespan (resources/pools), middleware (request-id), and the
exception handlers that turn the error taxonomy into uniform JSON responses.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import get_settings
from .errors import AgenticError, DependencyUnavailable
from .jobs import JobRunner
from .logging_config import (get_logger, new_request_id, set_request_id,
                             setup_logging)
from .pipeline import init_stage_executor, shutdown_stage_executor
from .resources import Resources
from .routers import health as health_router
from .routers import pipeline as pipeline_router

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    app.state.settings = settings
    app.state.started_at = time.time()

    # stage executor sized for concurrent jobs + timed-out orphans
    init_stage_executor(max_workers=max(4, settings.max_concurrency * 3))

    # resources — if PG is down at startup, stay up (degraded); /ready reports it
    try:
        app.state.resources = Resources(settings)
        app.state.resources.bedrock.warm()  # validate OIDC early (non-fatal)
    except DependencyUnavailable as exc:
        log.error("startup: resources unavailable (%s) — running degraded", exc)
        app.state.resources = None

    app.state.job_runner = (JobRunner(app.state.resources, settings)
                            if app.state.resources else None)
    log.info("%s ready", settings.app_name)
    try:
        yield
    finally:
        shutdown_stage_executor()
        if app.state.resources:
            app.state.resources.close()


def create_app() -> FastAPI:
    app = FastAPI(title="planner-api", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        set_request_id(rid)
        try:
            response = await call_next(request)
        finally:
            pass
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(AgenticError)
    async def agentic_handler(request: Request, exc: AgenticError):
        from .logging_config import request_id_var
        rid = request_id_var.get()
        headers = {}
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        if exc.http_status >= 500:
            log.error("%s [%s] %s", exc.code, exc.stage, exc.message)
        return JSONResponse(status_code=exc.http_status,
                            content=exc.to_body(rid), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        from .logging_config import request_id_var
        return JSONResponse(status_code=422, content={"error": {
            "code": "validation_error", "message": "invalid request",
            "stage": "request", "request_id": request_id_var.get(),
            "retryable": False, "details": exc.errors()}})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        from .logging_config import request_id_var
        rid = request_id_var.get()
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"error": {
            "code": "internal_error", "message": "internal server error",
            "stage": None, "request_id": rid, "retryable": False}})

    # guard: pipeline endpoint needs live resources
    @app.middleware("http")
    async def require_resources_mw(request: Request, call_next):
        if request.url.path.startswith("/v1/") and \
                getattr(request.app.state, "job_runner", None) is None:
            from .logging_config import request_id_var
            return JSONResponse(status_code=503, content={"error": {
                "code": "dependency_unavailable",
                "message": "service not ready (resources unavailable)",
                "stage": "startup", "request_id": request_id_var.get(),
                "retryable": True}})
        return await call_next(request)

    app.include_router(health_router.router)
    app.include_router(pipeline_router.router)
    return app


app = create_app()
