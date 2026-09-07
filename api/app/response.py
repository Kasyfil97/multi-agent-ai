"""Uniform response envelope + a synchronous operation runner.

Every endpoint returns the same shape::

    {status_code, response_code, error_message,
     data: {request_id, started_at, finished_at, result}}

Timestamps are rendered at UTC+7 (WIB). ``run_operation`` executes a blocking
function in a worker thread (so the event loop stays responsive) and wraps its
outcome — or a mapped ``AgenticError`` — into the envelope.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .errors import AgenticError
from .logging_config import set_request_id

# WIB (Asia/Jakarta) — response timestamps are rendered at UTC+7.
WIB = timezone(timedelta(hours=7))


def iso_wib(ts: Optional[float]) -> Optional[str]:
    """Format an epoch seconds value as an ISO-8601 string at UTC+7, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=WIB).isoformat()


def envelope(*, status_code: int, response_code: str, error_message: Optional[str],
             request_id: Optional[str], started_at: Optional[float],
             finished_at: Optional[float], result) -> dict:
    return {
        "status_code": status_code,
        "response_code": response_code,
        "error_message": error_message,
        "data": {
            "request_id": request_id,
            "started_at": iso_wib(started_at),
            "finished_at": iso_wib(finished_at),
            "result": result,
        },
    }


async def run_operation(request_id: str, fn: Callable, *args) -> dict:
    """Run a blocking ``fn(*args)`` in a worker thread; wrap result/errors in the envelope."""
    started = time.time()

    def _blocking():
        set_request_id(request_id)
        return fn(*args)

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _blocking)
        code, rc, msg = 200, "succeeded", None
    except AgenticError as e:
        result, code, rc, msg = None, e.http_status, e.code, e.message
    except Exception as e:  # noqa: BLE001
        result, code, rc, msg = None, 500, "internal_error", str(e)
    return envelope(status_code=code, response_code=rc, error_message=msg,
                    request_id=request_id, started_at=started,
                    finished_at=time.time(), result=result)
