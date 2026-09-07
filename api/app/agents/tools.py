"""Strands tools for the agents, backed by the Postgres schema catalog.

The tools are called by the agent loop with only their declared args, so the shared
``PGPool`` is bound once at startup via ``bind_pool``. Tools NEVER raise inside the
agent loop — DB failures are caught and returned as a short message so the agent can
proceed / degrade gracefully.
"""
from __future__ import annotations

from strands import tool

from ..logging_config import get_logger
from ..services.schema import (format_columns, format_schema_tables,
                               get_table_columns, search_schema_tables as _search)

log = get_logger("agents.tools")
_POOL = None


def bind_pool(pool) -> None:
    """Bind the shared PGPool used by the tools (called at app startup)."""
    global _POOL
    _POOL = pool


@tool
def search_schema_tables(query: str) -> str:
    """Cari tabel sumber data yang relevan dari katalog skema database BRI.

    Gunakan tool ini ketika tiket analis serupa tidak ada atau tidak relevan,
    untuk menemukan tabel & kolom nyata yang bisa dipakai menyusun rencana.

    Args:
        query: kata kunci / deskripsi data yang dicari, mis.
            "rekening dormant saldo tabungan per unit kerja".
    """
    if _POOL is None:
        return "(pencarian skema tidak tersedia)"
    try:
        with _POOL.borrow() as conn:
            return format_schema_tables(_search(query, conn, k=5))
    except Exception as exc:  # noqa: BLE001 — never break the agent loop
        log.warning("search_schema_tables failed: %s", exc)
        return f"(gagal mencari skema: {type(exc).__name__})"


@tool
def lookup_columns(table: str) -> str:
    """Ambil daftar kolom NYATA (nama, tipe, arti) sebuah tabel dari katalog skema.

    Gunakan untuk memastikan nama kolom benar sebelum menulisnya di SQL — jangan
    menebak nama kolom.

    Args:
        table: nama tabel (boleh dengan/atau tanpa skema, mis. "savingmaster_daily").
    """
    if _POOL is None:
        return "(lookup kolom tidak tersedia)"
    try:
        with _POOL.borrow() as conn:
            return format_columns(table, get_table_columns(table, conn)[:60])
    except Exception as exc:  # noqa: BLE001
        log.warning("lookup_columns failed: %s", exc)
        return f"(gagal lookup kolom: {type(exc).__name__})"
