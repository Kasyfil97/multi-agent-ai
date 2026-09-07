"""Hybrid retrieval over ``era_corpus`` + confidence gate.

Best method from the retrieval research:
    score = 0.3 * zscore(dense_qvec)  +  0.7 * zscore(sparse_enriched)
dense = multi-vector max-pool cosine (query bge-m3 vs ``era_corpus_qvec``);
sparse = enriched BM25 (query TF vs ``era_corpus.sparse_enriched``).

If the embedding service is down, degrades to **sparse-only** BM25 (``degraded=True``).
Abstention gate: ``conf_sparse`` (raw enriched-BM25 of the top-1 ticket) ≥ threshold
→ ``relevant``; else candidates but ``no_relevant``; none → ``no_candidates``.
"""
from __future__ import annotations

from ..errors import DependencyUnavailable, RetrievalError
from ..logging_config import get_logger
from .fusion import POOL, sparse_literal, zscore_fuse

log = get_logger("services.retrieval")

CONTEXT_COLUMNS = [
    "id", "canonical_need", "tables", "query_engine", "key_filters",
    "report_codes", "domain_tags", "keywords", "has_solution", "solution",
    "solution_source", "analyst_notes",
]


def _bm25_meta(cur):
    cur.execute("SELECT value FROM era_corpus_bm25_enriched_meta WHERE key='dim'")
    dim = int(cur.fetchone()[0])
    cur.execute("SELECT token, idx FROM era_corpus_bm25_enriched")
    return dim, dict(cur.fetchall())


def _sparse_candidates(cur, lit, limit):
    if not lit:
        return {}
    cur.execute(
        "SELECT id, -(sparse_enriched <#> %s::sparsevec) AS s "
        "FROM era_corpus WHERE sparse_enriched IS NOT NULL "
        "ORDER BY sparse_enriched <#> %s::sparsevec LIMIT %s", (lit, lit, limit))
    return {r[0].upper(): float(r[1]) for r in cur.fetchall()}


def _dense_candidates(cur, qvec, limit):
    dvec = "[" + ",".join(f"{x:.8f}" for x in qvec) + "]"
    cur.execute(
        "SELECT issue_key, max(1 - (dense <=> %s::vector)) AS s "
        "FROM era_corpus_qvec GROUP BY issue_key ORDER BY s DESC LIMIT %s",
        (dvec, limit))
    return {r[0].upper(): float(r[1]) for r in cur.fetchall()}


def _fetch_tickets(cur, keys):
    if not keys:
        return []
    cols = ", ".join(CONTEXT_COLUMNS)
    cur.execute(f"SELECT {cols} FROM era_corpus WHERE id = ANY(%s)", (keys,))
    by_id = {}
    for row in cur.fetchall():
        rec = dict(zip(CONTEXT_COLUMNS, row))
        by_id[rec["id"].upper()] = rec
    return by_id


def retrieve_gated(question, pool, embed_client, k=5, threshold=38.0):
    """Returns dict: status, conf_sparse, threshold, retrieval_method, degraded,
    tickets, relevant_tickets. Raises RetrievalError on hard DB failure."""
    method = "hybrid"
    degraded = False

    # dense vector (optional; degrade on failure)
    qvec = None
    try:
        qvec = embed_client.embed_query(question)
    except DependencyUnavailable:
        method, degraded = "sparse_only", True
        log.warning("embedding down — sparse-only retrieval")

    try:
        with pool.borrow() as conn:
            cur = conn.cursor()
            cur.execute("SET hnsw.ef_search = 200")
            dim, vocab = _bm25_meta(cur)
            lit = sparse_literal(question, vocab, dim)
            sparse = _sparse_candidates(cur, lit, POOL if qvec is not None else k)
            dense = _dense_candidates(cur, qvec, POOL) if qvec is not None else {}
            hits = zscore_fuse(dense, sparse) if qvec is not None else \
                [{"id": i, "fused": s, "dense": None, "sparse": s}
                 for i, s in sorted(sparse.items(), key=lambda kv: -kv[1])[:k]]
            hits = hits[:max(k, 1)] if qvec is None else hits[:POOL]
            keys = [h["id"] for h in hits][: max(k, 1)] if qvec is None else \
                [h["id"] for h in hits]
            by_id = _fetch_tickets(cur, [h["id"] for h in hits])
    except DependencyUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(f"retrieval query failed: {exc}", stage="retrieval") from exc

    # hydrate top-k tickets in fused order
    tickets = []
    for h in hits:
        rec = by_id.get(h["id"])
        if not rec:
            continue
        rec = dict(rec)
        rec["retrieval_score"] = h["fused"]
        rec["conf_sparse_raw"] = h["sparse"]
        rec["retrieval_method"] = method
        rec["above_threshold"] = (h["sparse"] is not None and h["sparse"] >= threshold)
        tickets.append(rec)
        if len(tickets) >= k:
            break
    for rank, t in enumerate(tickets, 1):
        t["rank"] = rank

    conf_sparse = tickets[0]["conf_sparse_raw"] if tickets else None
    relevant = [t for t in tickets if t["above_threshold"]]
    status = "no_candidates" if not tickets else \
        ("no_relevant" if not relevant else "relevant")

    return {"status": status, "conf_sparse": conf_sparse, "threshold": threshold,
            "retrieval_method": method, "degraded": degraded,
            "tickets": tickets, "relevant_tickets": relevant}


def slice_gate(gate, k):
    """Top-k view of a gate (status/conf come from top-1)."""
    tickets = gate["tickets"][:k]
    return {"status": gate["status"], "conf_sparse": gate["conf_sparse"],
            "threshold": gate["threshold"], "retrieval_method": gate["retrieval_method"],
            "degraded": gate.get("degraded", False), "tickets": tickets,
            "relevant_tickets": [t for t in tickets if t.get("above_threshold")]}


def _fmt_list(v):
    if not v:
        return "-"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if v else "-"
    return str(v)


def format_context(tickets, max_solution_chars=1800, show_relevance=True):
    """Render retrieved tickets into a compact block for the planner prompt."""
    if not tickets:
        return "(tidak ada tiket serupa yang ditemukan)"
    blocks = []
    for t in tickets:
        sol = (t.get("solution") or "").strip()
        if len(sol) > max_solution_chars:
            sol = sol[:max_solution_chars] + "\n... [dipotong]"
        if not t.get("has_solution") or not sol:
            sol = "(tidak ada solusi tersimpan)"
        notes = (t.get("analyst_notes") or "").strip() or "-"
        flag = ""
        if show_relevance and "above_threshold" in t:
            cs = t.get("conf_sparse_raw")
            cs_s = f"{cs:.1f}" if cs is not None else "n/a"
            flag = (f"  [RELEVAN, conf_sparse={cs_s}]" if t["above_threshold"]
                    else f"  [DI BAWAH AMBANG, conf_sparse={cs_s} — mungkin tak relevan]")
        blocks.append(
            f"### Tiket #{t['rank']}: {t['id']}"
            f"  (skor retrieval={t.get('retrieval_score'):.3f}){flag}\n"
            f"- Kebutuhan (canonical_need): {t.get('canonical_need') or '-'}\n"
            f"- Tabel sumber (tables): {_fmt_list(t.get('tables'))}\n"
            f"- Query engine: {t.get('query_engine') or '-'}\n"
            f"- Key filters: {_fmt_list(t.get('key_filters'))}\n"
            f"- Report codes: {_fmt_list(t.get('report_codes'))}\n"
            f"- Domain tags: {_fmt_list(t.get('domain_tags'))}\n"
            f"- Keywords: {_fmt_list(t.get('keywords'))}\n"
            f"- Catatan analis (analyst_notes): {notes}\n"
            f"- Solusi ({t.get('solution_source') or 'n/a'}):\n```sql\n{sol}\n```")
    return "\n\n".join(blocks)
