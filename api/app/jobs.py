"""Synchronous execution of the (long-running) pipeline.

The heavy pipeline (30–120s, multiple LLM calls) runs in a worker thread so the event
loop stays responsive, but the HTTP request blocks until it finishes — there is no
queue or background polling. A per-request ``Job`` just carries timing/status for the
response payload.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from .errors import AgenticError
from .logging_config import get_logger, set_request_id
from .pipeline import StageLog, run_pipeline
from .response import envelope

log = get_logger("jobs")


@dataclass
class Job:
    goal: str
    k: int
    request_id: str
    status: str = "running"          # running|succeeded|partial|failed
    status_code: int = 200           # HTTP-style outcome code
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    stages: StageLog = field(default_factory=StageLog)
    result: Optional[dict] = None
    error: Optional[dict] = None
    warnings: list = field(default_factory=list)

    def to_response(self) -> dict:
        """Envelope: {status_code, response_code, error_message, data}."""
        is_error = self.status == "failed"
        return envelope(
            status_code=self.status_code,
            response_code=self.error["code"] if is_error else self.status,
            error_message=self.error["message"] if is_error else None,
            request_id=self.request_id,
            started_at=self.started_at,
            finished_at=self.finished_at,
            result=self.result.get("sql") if self.result else None,
        )


class JobRunner:
    def __init__(self, resources, settings):
        self.resources = resources
        self.settings = settings

    def _blocking(self, job: Job):
        set_request_id(job.request_id)
        return run_pipeline(job, self.resources, self.settings, job.stages)

    async def run(self, job: Job) -> Job:
        """Execute the pipeline to completion, populating ``job`` in place."""
        job.status = "running"
        job.started_at = time.time()
        set_request_id(job.request_id)
        log.info("job started: %.80s", job.goal)
        loop = asyncio.get_running_loop()
        try:
            result, warnings, overall = await loop.run_in_executor(
                None, self._blocking, job)
            job.result, job.warnings, job.status = result, warnings, overall
        except AgenticError as e:
            job.status = "failed"
            job.status_code = e.http_status
            job.error = e.to_body(job.request_id)["error"]
            log.warning("job failed: %s", e.code)
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.status_code = 500
            job.error = {"code": "internal_error", "message": str(e),
                         "stage": None, "request_id": job.request_id,
                         "retryable": False}
            log.exception("job crashed")
        finally:
            job.finished_at = time.time()
            log.info("job finished: %s", job.status)
        return job
