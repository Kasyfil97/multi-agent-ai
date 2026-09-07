"""Pipeline orchestration: retrieval → plan → sql → validate (NO evaluation).

Each stage runs with a timeout (via a shared executor) and bounded retry for
*transient* errors only. Critical stages (retrieval, plan) failing → the whole job
fails. Non-critical stages (sql, validate) failing → ``partial`` result with the
field left null + a warning. A per-job wall-clock cap guards runaway agent cost.
"""
from __future__ import annotations

import concurrent.futures as cf
import time

from .config import Settings
from .errors import AgenticError, JobTimeout, UpstreamTimeout, to_agentic
from .logging_config import get_logger
from .agents.planner import generate_plan
from .agents.sql_writer import generate_sql
from .services.retrieval import retrieve_gated, slice_gate
from .services.sql_validator import validate_sql

log = get_logger("pipeline")
_STAGE_EXEC: cf.ThreadPoolExecutor | None = None


def init_stage_executor(max_workers: int) -> None:
    global _STAGE_EXEC
    _STAGE_EXEC = cf.ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="stage")


def shutdown_stage_executor() -> None:
    if _STAGE_EXEC is not None:
        _STAGE_EXEC.shutdown(wait=False, cancel_futures=True)


def run_stage(name, fn, *, timeout, retries, backoff):
    """Run ``fn`` with a timeout + transient-retry. Raises an AgenticError on failure."""
    assert _STAGE_EXEC is not None, "stage executor not initialized"
    last = None
    for attempt in range(retries + 1):
        fut = _STAGE_EXEC.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except cf.TimeoutError:
            fut.cancel()
            raise UpstreamTimeout(f"stage '{name}' exceeded {timeout}s", stage=name)
        except Exception as exc:  # noqa: BLE001
            err = to_agentic(exc, stage=name)
            last = err
            if err.retryable and attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                log.warning("stage %s retry %d after %s", name, attempt + 1, err.code)
                continue
            raise err
    raise last  # pragma: no cover


class StageLog(dict):
    """Records per-stage {status, ms, ...} and timings."""

    def record(self, name, *, status, ms, **extra):
        self[name] = {"status": status, "ms": round(ms, 1), **extra}


def run_pipeline(req, resources, settings: Settings, stages: StageLog):
    """Execute the full pipeline. Returns (result_dict, warnings, overall_status).

    Raises AgenticError for critical-stage failures (caller marks job 'failed')."""
    warnings: list[str] = []
    deadline = time.monotonic() + settings.job_max_seconds

    def guard():
        if time.monotonic() > deadline:
            raise JobTimeout(f"job exceeded {settings.job_max_seconds}s wall-clock")

    def timed(name, fn, timeout, retries):
        t0 = time.monotonic()
        try:
            out = run_stage(name, fn, timeout=timeout, retries=retries,
                            backoff=settings.backoff_base)
            stages.record(name, status="ok", ms=(time.monotonic() - t0) * 1000)
            return out
        except AgenticError as e:
            stages.record(name, status="error", ms=(time.monotonic() - t0) * 1000,
                          error=e.code)
            raise

    with resources.bedrock.acquire() as client:
        # 1. retrieval (CRITICAL)
        gate_full = timed(
            "retrieval",
            lambda: retrieve_gated(req.goal, resources.pg, resources.embed,
                                   k=req.k, threshold=settings.conf_threshold),
            settings.retrieval_timeout, settings.retries)
        if gate_full.get("degraded"):
            warnings.append("retrieval degraded to sparse_only (embedding unavailable)")
            stages["retrieval"]["degraded"] = True
        gate = slice_gate(gate_full, req.k)
        guard()

        # 2. plan (CRITICAL)
        plan = timed("plan", lambda: generate_plan(req.goal, gate, client, resources.pg),
                     settings.plan_timeout, settings.retries)
        guard()

        result = {
            "status": gate["status"], "conf_sparse": gate["conf_sparse"],
            "retrieval_method": gate["retrieval_method"],
            "degraded": gate.get("degraded", False),
            "n_relevant": len(gate["relevant_tickets"]),
            "dialect": None, "declined": None, "sql": None,
        }
        overall = "succeeded"

        # 3. sql (NON-CRITICAL → partial on failure)
        try:
            sql_out = timed("sql",
                            lambda: generate_sql(req.goal, plan, gate, client),
                            settings.sql_timeout, settings.retries)
            result.update({"sql": sql_out["sql"], "dialect": sql_out["dialect"],
                           "declined": sql_out["declined"]})
        except AgenticError as e:
            warnings.append(f"sql stage failed ({e.code}): {e.message}")
            overall = "partial"
            sql_out = None

        # 4. validate (deterministic, inline — internal guard, NOT in payload)
        if sql_out is not None:
            t0 = time.monotonic()
            try:
                with resources.pg.borrow() as conn:
                    v = validate_sql(sql_out["sql"], sql_out["dialect"], conn)
                stages.record("validate", status="ok",
                              ms=(time.monotonic() - t0) * 1000,
                              parses=v.get("parses"))
                if not v.get("parses"):
                    warnings.append("generated SQL did not parse (sqlglot)")
            except Exception as e:  # noqa: BLE001
                stages.record("validate", status="error",
                              ms=(time.monotonic() - t0) * 1000,
                              error=type(e).__name__)
                warnings.append(f"validate skipped: {type(e).__name__}")

    return result, warnings, overall
