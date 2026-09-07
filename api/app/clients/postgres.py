"""Postgres connection pool (thread-safe) for retrieval, tools, and validation.

Wraps ``psycopg2.pool.ThreadedConnectionPool`` with a ``borrow()`` context manager.
Connection settings come from ``Settings``; ``PG_KB_SCHEMA`` is prepended to the
search_path when not ``public`` (mirrors the original ``pg_config``).
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as pg_pool

from ..config import Settings
from ..errors import DependencyUnavailable
from ..logging_config import get_logger

log = get_logger("clients.postgres")


def pg_dsn_kwargs(s: Settings) -> dict:
    cfg = {"host": s.pg_host, "port": s.pg_port, "dbname": s.pg_dbname,
           "user": s.pg_user, "password": s.pg_password, "connect_timeout": 20}
    schema = (s.pg_kb_schema or "").strip()
    if schema and schema != "public":
        cfg["options"] = f"-c search_path={schema},public"
    return cfg


class PGPool:
    def __init__(self, settings: Settings):
        self.s = settings
        try:
            self._pool = pg_pool.ThreadedConnectionPool(
                settings.pg_pool_min, settings.pg_pool_max, **pg_dsn_kwargs(settings))
        except psycopg2.Error as exc:
            raise DependencyUnavailable(f"Postgres pool init failed: {exc}",
                                        stage="startup") from exc
        log.info("PG pool ready (min=%s max=%s)", settings.pg_pool_min,
                 settings.pg_pool_max)

    @contextmanager
    def borrow(self):
        """Yield a connection; always return it to the pool. Maps failures to
        ``DependencyUnavailable``."""
        conn = None
        try:
            conn = self._pool.getconn()
            yield conn
        except psycopg2.OperationalError as exc:
            raise DependencyUnavailable(f"Postgres unavailable: {exc}",
                                        stage="postgres") from exc
        finally:
            if conn is not None:
                try:
                    conn.rollback()  # never leak an open txn back to the pool
                except Exception:  # noqa: BLE001
                    pass
                self._pool.putconn(conn)

    def ping(self) -> bool:
        try:
            with self.borrow() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        try:
            self._pool.closeall()
        except Exception:  # noqa: BLE001
            pass
