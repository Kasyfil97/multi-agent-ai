"""API-level tests: routing, health/ready, job lifecycle, error mapping, degradation."""
from api.app import pipeline
from api.app.errors import RetrievalError, SQLWriterError
from api.app.services import schema_search


def _submit(c, goal="Berikan data rekening dormant per uker 31 Des 2025", **kw):
    body = {"goal": goal, **kw}
    return c.post("/v1/generate-sql", json=body)


def test_health(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "X-Request-ID" in r.headers


def test_ready(client):
    c, _ = client
    r = c.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["checks"]["postgres"] is True


def test_validation_error_empty_goal(client):
    c, _ = client
    r = c.post("/v1/generate-sql", json={"goal": ""})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_sync_success(client):
    c, _ = client
    r = _submit(c)
    assert r.status_code == 200
    body = r.json()
    assert body["status_code"] == 200
    assert body["response_code"] == "succeeded"
    assert body["error_message"] is None
    data = body["data"]
    assert set(data) == {"request_id", "started_at", "finished_at", "result"}
    assert data["result"].upper().startswith("SELECT")


def test_partial_on_sql_failure(client):
    c, mp = client
    def boom(goal, plan, gate, client):
        raise SQLWriterError("no usable query", stage="sql")
    mp.setattr(pipeline, "generate_sql", boom)
    r = _submit(c)
    assert r.status_code == 200
    body = r.json()
    assert body["status_code"] == 200
    assert body["response_code"] == "partial"
    assert body["data"]["result"] is None


def test_failed_on_critical_retrieval(client):
    c, mp = client
    def boom(goal, pool, embed, k=5, threshold=38.0):
        raise RetrievalError("pg down", stage="retrieval")
    mp.setattr(pipeline, "retrieve_gated", boom)
    r = _submit(c)
    assert r.status_code == 200          # HTTP always 200; outcome is in the envelope
    body = r.json()
    assert body["status_code"] == 503
    assert body["response_code"] == "retrieval_error"
    assert body["error_message"] == "pg down"
    assert body["data"]["result"] is None


def test_search_tables(client):
    c, _ = client
    r = c.post("/v1/search-tables", json={"query": "rekening dormant", "k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["status_code"] == 200
    assert body["response_code"] == "succeeded"
    data = body["data"]
    assert set(data) == {"request_id", "started_at", "finished_at", "result"}
    assert isinstance(data["result"], list)
    assert data["result"][0]["table_name"] == "savingmaster"
    assert "score" in data["result"][0]


def test_search_columns_with_table_filter(client):
    c, mp = client
    seen = {}
    def fake(query, pool, embed, k=10, table_name=None):
        seen["table_name"] = table_name
        return [{"table_name": "savingmaster", "field_name": "saldo",
                 "business_title": "Saldo", "data_type": "decimal",
                 "description": "", "score": 0.9}]
    mp.setattr(schema_search, "semantic_search_columns", fake)
    r = c.post("/v1/search-columns",
               json={"query": "saldo", "k": 5, "table_name": "savingmaster"})
    assert r.status_code == 200
    body = r.json()
    assert body["response_code"] == "succeeded"
    assert body["data"]["result"][0]["field_name"] == "saldo"
    assert seen["table_name"] == "savingmaster"   # filter forwarded to the service


def test_search_validation_error(client):
    c, _ = client
    r = c.post("/v1/search-tables", json={"query": "x"})   # below min_length=2
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_degraded_retrieval_still_succeeds(client):
    c, mp = client
    from api.tests.conftest import _fake_gate
    mp.setattr(pipeline, "retrieve_gated",
               lambda goal, pool, embed, k=5, threshold=38.0: _fake_gate(degraded=True))
    r = _submit(c)
    body = r.json()
    # degradation is handled internally and NOT surfaced in the trimmed payload
    assert body["response_code"] == "succeeded"
    assert body["data"]["result"].upper().startswith("SELECT")
