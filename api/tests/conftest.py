"""Test fixtures: an app whose heavy resources & stages are faked (no network/DB)."""
import contextlib

import pytest
from fastapi.testclient import TestClient

from api.app import main, pipeline
from api.app.errors import SQLWriterError
from api.app.services import schema_search


# --- fake resources --------------------------------------------------------
class _FakeBedrockPool:
    @contextlib.contextmanager
    def acquire(self, timeout=30):
        yield object()

    def warm(self):
        return True

    def ping(self):
        return True


class _FakePG:
    @contextlib.contextmanager
    def borrow(self):
        yield None

    def ping(self):
        return True

    def close(self):
        pass


class _FakeEmbed:
    def ping(self):
        return True


class FakeResources:
    def __init__(self, settings):
        self.pg = _FakePG()
        self.bedrock = _FakeBedrockPool()
        self.embed = _FakeEmbed()

    def readiness(self):
        return {"status": "ready",
                "checks": {"postgres": True, "embedding": True, "bedrock": True}}

    def close(self):
        pass


def _fake_gate(status="relevant", degraded=False):
    t = {"rank": 1, "id": "ERA25-1", "retrieval_score": 1.0, "conf_sparse_raw": 55.0,
         "above_threshold": status == "relevant", "tables": ["savingmaster"],
         "query_engine": "SparkSQL", "has_solution": True, "solution": "SELECT 1",
         "canonical_need": "x", "key_filters": [], "report_codes": [],
         "domain_tags": [], "keywords": [], "solution_source": "q", "analyst_notes": ""}
    rel = [t] if status == "relevant" else []
    return {"status": status, "conf_sparse": 55.0 if status == "relevant" else 20.0,
            "threshold": 38.0, "retrieval_method": "sparse_only" if degraded else "hybrid",
            "degraded": degraded, "tickets": [t], "relevant_tickets": rel}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "Resources", FakeResources)
    monkeypatch.setattr(pipeline, "retrieve_gated",
                        lambda goal, pool, embed, k=5, threshold=38.0: _fake_gate())
    monkeypatch.setattr(pipeline, "generate_plan",
                        lambda goal, gate, client, pool=None: "## 0. Klasifikasi\nPLAN")
    monkeypatch.setattr(pipeline, "generate_sql",
                        lambda goal, plan, gate, client: {
                            "sql": "SELECT a FROM savingmaster WHERE ds='20251231'",
                            "dialect": "spark", "engine": "SparkSQL",
                            "declined": False, "raw": ""})
    monkeypatch.setattr(pipeline, "validate_sql",
                        lambda sql, dialect, conn: {
                            "parses": True, "read_only": True, "static_score": 1.0,
                            "empty": False, "lint": []})
    monkeypatch.setattr(schema_search, "semantic_search_tables",
                        lambda query, pool, embed, k=10: [
                            {"table_name": "savingmaster", "source_schema": "public",
                             "domain_tags": ["saving"], "n_columns": 12,
                             "table_description": "rekening tabungan", "score": 1.23}])
    monkeypatch.setattr(schema_search, "semantic_search_columns",
                        lambda query, pool, embed, k=10, table_name=None: [
                            {"table_name": "savingmaster", "field_name": "saldo",
                             "business_title": "Saldo", "data_type": "decimal",
                             "description": "saldo rekening", "score": 0.98}])
    app = main.create_app()
    with TestClient(app) as c:
        yield c, monkeypatch


@pytest.fixture
def gate_helpers():
    return _fake_gate, FakeResources, SQLWriterError
