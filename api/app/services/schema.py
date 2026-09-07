"""Schema-catalog harness (Postgres): table search + column lookup + catalog load.

Backs the planner/SQL-writer tools and the validator. All BM25 here is pure
term-frequency over a stored vocab (no embedding service needed).

Data read (schema ``public``): ``schema_tables`` (+ ``schema_tables_bm25`` /
``_meta``), ``schema_columns``.
"""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[a-z0-9]+")

# process-wide catalog cache (read-only schema); (known_tables:set, table_columns:dict)
_CATALOG_CACHE = None


def _sparse_literal(text: str, vocab: dict, dim: int) -> str | None:
    counts: dict[int, int] = {}
    for t in TOKEN_RE.findall(text.lower()):
        j = vocab.get(t)
        if j:
            counts[j] = counts.get(j, 0) + 1
    if not counts:
        return None
    body = ",".join(f"{i}:{v}" for i, v in sorted(counts.items()))
    return "{" + body + "}/" + str(dim)


def search_schema_tables(query: str, conn, k: int = 5, max_cols_chars: int = 600):
    """Top-k schema tables most relevant to ``query`` (BM25). Empty list if no hit."""
    cur = conn.cursor()
    cur.execute("SET hnsw.ef_search = 200")
    cur.execute("SELECT value FROM schema_tables_bm25_meta WHERE key='dim'")
    dim = int(cur.fetchone()[0])
    cur.execute("SELECT token, idx FROM schema_tables_bm25")
    vocab = dict(cur.fetchall())
    lit = _sparse_literal(query, vocab, dim)
    if not lit:
        return []
    cur.execute(
        "SELECT id, table_name, source_schema, domain_tags, n_columns, "
        "table_description, columns_dict, -(sparse <#> %s::sparsevec) AS s "
        "FROM schema_tables WHERE sparse IS NOT NULL "
        "ORDER BY sparse <#> %s::sparsevec LIMIT %s", (lit, lit, k))
    out = []
    for r in cur.fetchall():
        cols = (r[6] or "").strip()
        if len(cols) > max_cols_chars:
            cols = cols[:max_cols_chars] + " ...[dipotong]"
        out.append({"id": r[0], "table_name": r[1], "source_schema": r[2],
                    "domain_tags": list(r[3] or []), "n_columns": r[4],
                    "table_description": r[5], "columns_dict": cols,
                    "score": float(r[7])})
    return out


def format_schema_tables(tables) -> str:
    if not tables:
        return "(tidak ada tabel schema yang cocok)"
    blocks = []
    for i, t in enumerate(tables, 1):
        blocks.append(
            f"### Tabel #{i}: {t['source_schema']}.{t['table_name']}  "
            f"(skor={t['score']:.1f}, {t['n_columns']} kolom)\n"
            f"- Domain: {', '.join(t['domain_tags']) or '-'}\n"
            f"- Deskripsi: {t['table_description']}\n"
            f"- Kolom: {t['columns_dict']}")
    return "\n\n".join(blocks)


def get_table_columns(table_name: str, conn, limit: int = 200):
    """Real columns for a table from ``schema_columns`` (exact then substring match)."""
    bare = str(table_name).split(".")[-1].strip().strip("[]`\"")
    cur = conn.cursor()
    cur.execute("SELECT field_name, business_title, data_type FROM schema_columns "
                "WHERE lower(table_name)=lower(%s) LIMIT %s", (bare, limit))
    rows = cur.fetchall()
    if not rows:
        cur.execute("SELECT field_name, business_title, data_type FROM schema_columns "
                    "WHERE table_name ILIKE %s LIMIT %s", (f"%{bare}%", limit))
        rows = cur.fetchall()
    return [{"field_name": r[0], "business_title": r[1], "data_type": r[2]}
            for r in rows]


def format_columns(table_name: str, cols) -> str:
    if not cols:
        return f"(tabel '{table_name}' tidak ditemukan di katalog schema_columns)"
    lines = [f"Kolom untuk `{table_name}` ({len(cols)}):"]
    for c in cols:
        lines.append(f"- {c['field_name']} ({c['data_type']}) — {c['business_title']}")
    return "\n".join(lines)


def load_catalog(conn):
    """(known_tables:set, table_columns:dict[str,set]) — bare lowercase names. Cached."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT table_name FROM schema_tables")
    tables = {r[0].split(".")[-1].lower() for r in cur.fetchall() if r[0]}
    cur.execute("SELECT table_name, field_name FROM schema_columns")
    table_columns: dict[str, set] = {}
    for tname, fname in cur.fetchall():
        if not tname:
            continue
        key = tname.split(".")[-1].lower()
        tables.add(key)
        table_columns.setdefault(key, set()).add((fname or "").lower())
    _CATALOG_CACHE = (tables, table_columns)
    return _CATALOG_CACHE
