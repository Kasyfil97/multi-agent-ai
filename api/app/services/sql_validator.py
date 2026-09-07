"""Static SQL validation with sqlglot — no data / no execution.

Validates structure + consistency with the schema catalog (all that's possible
offline): parse per dialect, extract tables/columns, catalog cross-check (advisory),
read-only + efficiency lint. Never raises — returns a report dict with a
``static_score`` (0-1). This is a deterministic validator, NOT an LLM evaluator.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from .schema import load_catalog

DDL_DML = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
           exp.Merge, exp.TruncateTable)
PARTITION_HINTS = ("ds", "position_date", "posisi", "tgl", "tanggal", "date",
                   "periode", "period", "snapshot", "as_of")


def _is_comment_only(sql: str) -> bool:
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    return body.strip() == ""


def validate_sql(sql: str, dialect: str = "spark", conn=None) -> dict:
    rep = {
        "dialect": dialect, "empty": not bool(sql and sql.strip()),
        "comment_only": False, "parses": False, "syntax_error": None,
        "read_only": True, "statements": 0, "tables": [], "columns": [],
        "unknown_tables": [], "unknown_columns": [], "has_select_star": False,
        "has_where": False, "has_partition_filter": False, "join_count": 0,
        "cross_join": False, "lint": [], "static_score": 0.0,
    }
    if rep["empty"]:
        rep["lint"].append("SQL kosong")
        return rep
    if _is_comment_only(sql):
        rep["comment_only"] = True
        rep["lint"].append("comment-only (writer declined)")
        return rep

    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
        rep["parses"] = True
        rep["statements"] = len(trees)
    except Exception as exc:  # noqa: BLE001
        rep["syntax_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        rep["lint"].append("parse gagal")
        return rep

    tables, columns = set(), set()
    for tree in trees:
        if isinstance(tree, DDL_DML) or any(tree.find_all(*DDL_DML)):
            rep["read_only"] = False
        for t in tree.find_all(exp.Table):
            tables.add(t.name.lower())
        for c in tree.find_all(exp.Column):
            if c.name:
                columns.add(c.name.lower())
        if list(tree.find_all(exp.Star)):
            rep["has_select_star"] = True
        wheres = list(tree.find_all(exp.Where))
        if wheres:
            rep["has_where"] = True
            wtext = " ".join(w.sql().lower() for w in wheres)
            rep["has_partition_filter"] = any(h in wtext for h in PARTITION_HINTS)
        joins = list(tree.find_all(exp.Join))
        rep["join_count"] += len(joins)
        for j in joins:
            if not j.args.get("on") and not j.args.get("using") and j.this:
                rep["cross_join"] = True

    rep["tables"] = sorted(tables)
    rep["columns"] = sorted(columns)

    # catalog cross-check (advisory) — only if a conn is available
    if conn is not None:
        try:
            known_tables, table_columns = load_catalog(conn)
            rep["unknown_tables"] = sorted(t for t in tables if t not in known_tables)
            known_cols = set()
            for t in tables:
                known_cols |= table_columns.get(t, set())
            if any(t in table_columns for t in tables):
                rep["unknown_columns"] = sorted(c for c in columns if c not in known_cols)
        except Exception:  # noqa: BLE001 — catalog check is best-effort
            pass

    if not rep["read_only"]:
        rep["lint"].append("BUKAN read-only (ada DDL/DML)")
    if rep["has_select_star"]:
        rep["lint"].append("SELECT * (hindari)")
    if not rep["has_where"]:
        rep["lint"].append("tidak ada WHERE (risiko full-scan)")
    elif not rep["has_partition_filter"]:
        rep["lint"].append("WHERE tanpa filter periode/partisi")
    if rep["cross_join"]:
        rep["lint"].append("cross join tanpa ON/USING")
    if rep["unknown_tables"]:
        rep["lint"].append(f"tabel tak terverifikasi: {rep['unknown_tables']}")
    if rep["unknown_columns"]:
        rep["lint"].append(f"kolom tak dikenal di katalog: {rep['unknown_columns'][:8]}")

    score = 0.0
    score += 0.30 if rep["parses"] else 0.0
    score += 0.15 if rep["read_only"] else 0.0
    score += 0.15 if not rep["unknown_tables"] else 0.0
    score += 0.15 if not rep["unknown_columns"] else 0.0
    score += 0.10 if not rep["has_select_star"] else 0.0
    score += 0.10 if rep["has_where"] else 0.0
    score += 0.05 if (rep["has_partition_filter"] and not rep["cross_join"]) else 0.0
    rep["static_score"] = round(score, 3)
    return rep
