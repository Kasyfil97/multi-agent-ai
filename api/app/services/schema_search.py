"""Semantic search over the schema catalog using hybrid fusion.

Same method as ticket retrieval: ``0.3*zscore(dense) + 0.7*zscore(sparse)`` where
dense = bge-m3 cosine (query vs ``schema_{tables,columns}.dense``) and sparse =
enriched-BM25 (query TF vs ``.sparse``). Degrades to sparse-only BM25 if the
embedding service is down. Column search can be scoped to one ``table_name``.
"""
from __future__ import annotations

from ..errors import DependencyUnavailable, RetrievalError
from ..logging_config import get_logger
from .fusion import POOL, sparse_literal, zscore_fuse

log = get_logger("services.schema_search")


def _qvec_literal(qvec) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in qvec) + "]"


def _embed(query, embed_client):
    """Dense query vector, or None (→ sparse-only) if the embedding service is down."""
    try:
        return embed_client.embed_query(query)
    except DependencyUnavailable:
        log.warning("embedding down — sparse-only schema search")
        return None


def _bm25(cur, meta_table, vocab_table):
    cur.execute(f"SELECT value FROM {meta_table} WHERE key='dim'")
    dim = int(cur.fetchone()[0])
    cur.execute(f"SELECT token, idx FROM {vocab_table}")
    return dim, dict(cur.fetchall())


def _sparse_candidates(cur, table, lit, limit, where="", params=()):
    if not lit:
        return {}
    cur.execute(
        f"SELECT id, -(sparse <#> %s::sparsevec) AS s FROM {table} "
        f"WHERE sparse IS NOT NULL {where} "
        f"ORDER BY sparse <#> %s::sparsevec LIMIT %s",
        (lit, *params, lit, limit))
    return {r[0]: float(r[1]) for r in cur.fetchall()}


def _dense_candidates(cur, table, qvec, limit, where="", params=()):
    dvec = _qvec_literal(qvec)
    cur.execute(
        f"SELECT id, (1 - (dense <=> %s::vector)) AS s FROM {table} "
        f"WHERE dense IS NOT NULL {where} "
        f"ORDER BY dense <=> %s::vector LIMIT %s",
        (dvec, *params, dvec, limit))
    return {r[0]: float(r[1]) for r in cur.fetchall()}


def _rank(qvec, sparse, dense, k):
    """Fuse (or sparse-only fallback) and return the top-k ids in ranked order."""
    if qvec is not None:
        hits = zscore_fuse(dense, sparse)
    else:
        hits = [{"id": i, "fused": s, "dense": None, "sparse": s}
                for i, s in sorted(sparse.items(), key=lambda kv: -kv[1])]
    return hits[:max(k, 1)]


def _fetch(cur, table, cols, ids):
    if not ids:
        return {}
    cur.execute(f"SELECT id, {', '.join(cols)} FROM {table} WHERE id = ANY(%s)", (ids,))
    return {r[0]: dict(zip(cols, r[1:])) for r in cur.fetchall()}


def _assemble(hits, rows):
    """Attach fused score, preserving ranked order; drop ids missing a row."""
    out = []
    for h in hits:
        rec = rows.get(h["id"])
        if rec is None:
            continue
        rec = dict(rec)
        rec["score"] = h["fused"]
        out.append(rec)
    return out


_TABLE_COLS = ("table_name", "source_schema", "domain_tags", "n_columns",
               "table_description")
_COLUMN_COLS = ("table_name", "field_name", "business_title", "data_type",
                "description")


def semantic_search_tables(query, pool, embed_client, k=10):
    """Top-k schema tables most relevant to ``query`` (hybrid fusion)."""
    qvec = _embed(query, embed_client)
    try:
        with pool.borrow() as conn:
            cur = conn.cursor()
            cur.execute("SET hnsw.ef_search = 200")
            dim, vocab = _bm25(cur, "schema_tables_bm25_meta", "schema_tables_bm25")
            lit = sparse_literal(query, vocab, dim)
            sparse = _sparse_candidates(cur, "schema_tables", lit,
                                        POOL if qvec is not None else k)
            dense = (_dense_candidates(cur, "schema_tables", qvec, POOL)
                     if qvec is not None else {})
            hits = _rank(qvec, sparse, dense, k)
            rows = _fetch(cur, "schema_tables", _TABLE_COLS, [h["id"] for h in hits])
    except DependencyUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(f"table search failed: {exc}", stage="search_tables") from exc
    result = _assemble(hits, rows)
    for r in result:
        r["domain_tags"] = list(r.get("domain_tags") or [])
    return result


def semantic_search_columns(query, pool, embed_client, k=10, table_name=None):
    """Top-k schema columns most relevant to ``query`` (hybrid fusion).

    ``table_name`` (optional) scopes the search to one table (bare name, exact/lower)."""
    qvec = _embed(query, embed_client)
    where, params = "", ()
    if table_name:
        bare = str(table_name).split(".")[-1].strip().strip("[]`\"")
        where, params = "AND lower(table_name) = lower(%s)", (bare,)
    try:
        with pool.borrow() as conn:
            cur = conn.cursor()
            cur.execute("SET hnsw.ef_search = 200")
            dim, vocab = _bm25(cur, "schema_columns_bm25_meta", "schema_columns_bm25")
            lit = sparse_literal(query, vocab, dim)
            sparse = _sparse_candidates(cur, "schema_columns", lit,
                                        POOL if qvec is not None else k, where, params)
            dense = (_dense_candidates(cur, "schema_columns", qvec, POOL, where, params)
                     if qvec is not None else {})
            hits = _rank(qvec, sparse, dense, k)
            rows = _fetch(cur, "schema_columns", _COLUMN_COLS, [h["id"] for h in hits])
    except DependencyUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(f"column search failed: {exc}", stage="search_columns") from exc
    return _assemble(hits, rows)
