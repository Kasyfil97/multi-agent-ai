"""Endpoints: generate SQL (agentic pipeline) + semantic search over the schema catalog.

All three run synchronously and return the same uniform envelope (``JobResponse``)."""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import BadRequestError
from ..jobs import Job
from ..logging_config import request_id_var
from ..response import run_operation
from ..schemas import (GenerateSqlRequest, JobResponse, SearchColumnsRequest,
                       SearchTablesRequest)
from ..services import schema_search

router = APIRouter(prefix="/v1", tags=["pipeline"])


@router.post("/generate-sql", response_model=JobResponse)
async def generate_sql(req: GenerateSqlRequest, request: Request):
    if not req.goal.strip():
        raise BadRequestError("goal is empty", stage="request")

    runner = request.app.state.job_runner
    rid = request_id_var.get()

    job = Job(goal=req.goal.strip(), k=req.k, request_id=rid)
    await runner.run(job)
    return JobResponse(**job.to_response())


@router.post("/search-tables", response_model=JobResponse)
async def search_tables(req: SearchTablesRequest, request: Request):
    res = request.app.state.resources
    rid = request_id_var.get()
    body = await run_operation(rid, schema_search.semantic_search_tables,
                               req.query, res.pg, res.embed, req.k)
    return JobResponse(**body)


@router.post("/search-columns", response_model=JobResponse)
async def search_columns(req: SearchColumnsRequest, request: Request):
    res = request.app.state.resources
    rid = request_id_var.get()
    body = await run_operation(rid, schema_search.semantic_search_columns,
                               req.query, res.pg, res.embed, req.k, req.table_name)
    return JobResponse(**body)
