"""Unit tests for error classification and the sqlglot validator (no network)."""
from api.app.errors import (UpstreamRateLimited, classify, is_expired_credentials,
                                    to_agentic)
from api.app.services.sql_validator import validate_sql


def test_classify_transient():
    assert classify(ConnectionError("refused")) is True
    assert classify(TimeoutError("slow")) is True
    assert classify(RuntimeError("HTTP 503 service unavailable")) is True
    assert classify(RuntimeError("ThrottlingException: rate")) is True


def test_classify_permanent():
    assert classify(ValueError("bad input")) is False
    assert classify(RuntimeError("AccessDenied 403")) is False


def test_expired_credentials():
    assert is_expired_credentials(RuntimeError("ExpiredTokenException: expired"))
    assert not is_expired_credentials(RuntimeError("nope"))


def test_to_agentic_rate_limit():
    e = to_agentic(RuntimeError("429 TooManyRequests"), stage="bedrock")
    assert isinstance(e, UpstreamRateLimited)
    assert e.stage == "bedrock"


def test_validator_valid_spark():
    rep = validate_sql(
        "SELECT a.acctno, a.saldo FROM savingmaster a WHERE a.ds='20251231'",
        "spark", conn=None)
    assert rep["parses"] is True
    assert rep["read_only"] is True
    assert rep["has_where"] is True
    assert rep["has_partition_filter"] is True
    assert not rep["has_select_star"]
    assert rep["static_score"] > 0.6


def test_validator_syntax_error():
    # unbalanced parenthesis reliably fails the parser -> reported, never raised
    rep = validate_sql("SELECT * FROM (SELECT a FROM t", "spark", conn=None)
    assert rep["parses"] is False
    assert rep["syntax_error"]


def test_validator_backtick_quoted_ok():
    rep = validate_sql("SELECT x FROM `6969_crm_dim_dwh_branch` WHERE ds='1'",
                       "spark", conn=None)
    assert rep["parses"] is True
    assert "6969_crm_dim_dwh_branch" in rep["tables"]


def test_validator_rejects_ddl():
    rep = validate_sql("DELETE FROM t WHERE x=1", "spark", conn=None)
    assert rep["read_only"] is False
    assert any("read-only" in l for l in rep["lint"])


def test_validator_comment_only_decline():
    rep = validate_sql("-- TIDAK DAPAT MEMBUAT SQL: butuh klarifikasi", "spark", conn=None)
    assert rep["comment_only"] is True
    assert rep["parses"] is False
