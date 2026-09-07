"""SQL Writer agent — Strands ``Agent`` over gpt-oss-120b. Plan → one SQL query.

Dialect from the ticket ``query_engine``; grounded via the ``lookup_columns`` tool +
ticket solutions as few-shot. Robust to gpt-oss tool-path quirks (empty turn /
tool-literal leaking into the SQL block): retries, then a tool-less fallback.
Raises ``SQLWriterError`` if no usable output is produced.
"""
from __future__ import annotations

import re

from strands import Agent

from ..errors import SQLWriterError
from ..logging_config import get_logger
from .prompts import SQL_WRITER_SYSTEM_PROMPT
from .strands_model import StrandsBedrockModel
from .tools import lookup_columns as lookup_columns_tool

log = get_logger("agents.sql_writer")

ENGINE_TO_DIALECT = {
    "sqlserver": "tsql", "mssql": "tsql", "sql server": "tsql",
    "spark": "spark", "sparksql": "spark", "pyspark": "spark", "hive": "hive",
    "presto": "presto", "trino": "trino", "postgres": "postgres",
    "postgresql": "postgres", "oracle": "oracle", "mysql": "mysql",
}
DEFAULT_DIALECT = "spark"
_SQL_BLOCK = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def build_writer(client, *, max_tokens=2600, with_tools=True):
    model = StrandsBedrockModel(client, max_tokens=max_tokens)
    tools = [lookup_columns_tool] if with_tools else []
    return Agent(model=model, tools=tools, system_prompt=SQL_WRITER_SYSTEM_PROMPT)


def resolve_dialect(gate):
    tickets = gate.get("relevant_tickets") or gate.get("tickets") or []
    for t in tickets:
        eng = (t.get("query_engine") or "").strip().lower()
        if eng in ENGINE_TO_DIALECT:
            return ENGINE_TO_DIALECT[eng], t.get("query_engine")
    return DEFAULT_DIALECT, None


def _few_shot(gate, max_examples=2, max_chars=1200):
    tickets = (gate.get("relevant_tickets") or [])[:max_examples]
    blocks = []
    for t in tickets:
        sol = (t.get("solution") or "").strip()
        if not sol:
            continue
        if len(sol) > max_chars:
            sol = sol[:max_chars] + "\n-- ...[dipotong]"
        blocks.append(f"-- contoh solusi tiket {t.get('id')} "
                      f"(engine={t.get('query_engine')}):\n{sol}")
    return "\n\n".join(blocks) if blocks else "(tidak ada contoh solusi)"


def _looks_like_sql(sql):
    if not sql:
        return False
    up = sql.upper()
    if "TIDAK DAPAT" in up:
        return True
    return "SELECT" in up or up.lstrip().startswith("WITH")


def extract_sql(text):
    blocks = _SQL_BLOCK.findall(text or "")
    for b in reversed(blocks):
        b = b.strip()
        if _looks_like_sql(b):
            return b
    if blocks:
        return blocks[-1].strip()
    return (text or "").strip()


def build_prompt(goal, plan, gate, dialect):
    status = gate.get("status", "relevant")
    if status == "relevant":
        directive = (
            "Rencana ini GROUNDED ke tiket nyata. Anda WAJIB menghasilkan satu query "
            "SELECT yang mengadaptasi rencana & contoh solusi. JANGAN menolak. CATATAN: "
            "katalog `lookup_columns` TIDAK lengkap (hanya sebagian tabel) — ketiadaan "
            "tabel/kolom di katalog BUKAN alasan menolak. Gunakan nama kolom dari "
            "RENCANA dan CONTOH SOLUSI; bila tak terverifikasi, pakai nama paling "
            "mungkin + tandai `-- asumsi`. Tolak HANYA jika rencana sendiri tidak "
            "menyebut tabel/kolom konkret apa pun.")
    else:
        directive = (
            "Status retrieval BUKAN relevant: susun query terbaik dari tabel kandidat "
            "di rencana. Bila benar-benar tak ada tabel yang cocok / di luar domain, "
            "keluarkan komentar `-- TIDAK DAPAT MEMBUAT SQL: ...`.")
    return (
        f"# DIALEK TARGET\n{dialect}\n\n"
        f"# GOAL USER\n{goal}\n\n"
        f"# RENCANA (dari planner)\n{plan}\n\n"
        f"# CONTOH SOLUSI SERUPA (referensi, jangan disalin buta)\n{_few_shot(gate)}\n\n"
        f"{directive}\n"
        "Keluarkan HANYA satu query SQL final di dalam blok ```sql```. Di dalam blok "
        "itu HARUS ada pernyataan SELECT — JANGAN menuliskan nama tool sebagai isi SQL.")


def generate_sql(goal, plan, gate, client):
    """Return {sql, dialect, engine, declined, raw}. Raises SQLWriterError if unusable."""
    dialect, engine = resolve_dialect(gate)
    prompt = build_prompt(goal, plan, gate, dialect)

    agent = build_writer(client, with_tools=True)
    raw = str(agent(prompt)).strip()
    sql = extract_sql(raw)
    if not _looks_like_sql(sql):
        raw = str(agent("Keluarkan HANYA query SQL SELECT final di blok ```sql```. "
                        "Jangan menuliskan nama tool sebagai SQL, jangan kosong.")).strip()
        sql = extract_sql(raw)
    if not _looks_like_sql(sql):
        fb = build_writer(client, with_tools=False)
        raw = str(fb(prompt)).strip()
        sql = extract_sql(raw)
    if not _looks_like_sql(sql):
        raise SQLWriterError("SQL writer produced no usable query", stage="sql")

    declined = "TIDAK DAPAT" in sql.upper()
    return {"sql": sql, "dialect": dialect, "engine": engine,
            "declined": declined, "raw": raw}
