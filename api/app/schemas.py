"""Pydantic request/response models for the API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class GenerateSqlRequest(BaseModel):
    goal: str = Field(..., min_length=3, max_length=4000,
                      description="Permintaan data analis (natural language).")
    k: int = Field(10, ge=1, le=20, description="Jumlah tiket konteks retrieval.")


class SearchTablesRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000,
                       description="Kueri semantik untuk mencari tabel.")
    k: int = Field(10, ge=1, le=50, description="Jumlah tabel teratas.")


class SearchColumnsRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=2000,
                       description="Kueri semantik untuk mencari kolom.")
    k: int = Field(10, ge=1, le=100, description="Jumlah kolom teratas.")
    table_name: Optional[str] = Field(
        None, description="Batasi pencarian ke satu tabel (opsional).")


class ResponseData(BaseModel):
    request_id: Optional[str] = None
    started_at: Optional[str] = None     # ISO-8601 datetime @ UTC+7 (WIB)
    finished_at: Optional[str] = None    # ISO-8601 datetime @ UTC+7 (WIB)
    result: Optional[Any] = None         # SQL string (generate) or list (search)


class JobResponse(BaseModel):
    """Uniform envelope. ``response_code`` is the job status on success
    (succeeded|partial) or the error code on failure; ``status_code`` is the
    HTTP-style outcome code."""
    status_code: int
    response_code: str
    error_message: Optional[str] = None
    data: Optional[ResponseData] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_s: float


class ReadyResponse(BaseModel):
    status: str                       # ready | degraded | unavailable
    checks: dict[str, bool]
