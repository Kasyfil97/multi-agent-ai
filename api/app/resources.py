"""Shared, long-lived resources created at startup and torn down at shutdown.

- ``PGPool`` — shared, thread-safe connection pool.
- ``EmbeddingClient`` — dense encoder for retrieval (degrades if down).
- ``BedrockClientPool`` — bounded pool of thread-safe ``BedrockClient`` (OIDC is
  expensive; clients are NOT shared concurrently — each job acquires one exclusively).

Also provides readiness probes for ``/ready``.
"""
from __future__ import annotations

import queue
import threading
from contextlib import contextmanager

from .clients.bedrock import BedrockClient
from .clients.embeddings import EmbeddingClient
from .clients.postgres import PGPool
from .config import Settings
from .errors import DependencyUnavailable
from .logging_config import get_logger
from . import agents

log = get_logger("resources")


class BedrockClientPool:
    """Lazily-grown bounded pool. Each acquire hands out an exclusive client."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.size = max(1, settings.bedrock_pool_size)
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._created = 0
        self._lock = threading.Lock()

    def _make(self) -> BedrockClient:
        return BedrockClient(self.settings)

    @contextmanager
    def acquire(self, timeout: float = 30.0):
        client = None
        try:
            client = self._free.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self.size:
                    self._created += 1
                    make_new = True
                else:
                    make_new = False
            if make_new:
                try:
                    client = self._make()
                except Exception:
                    with self._lock:
                        self._created -= 1
                    raise
            else:
                try:
                    client = self._free.get(timeout=timeout)
                except queue.Empty as exc:
                    raise DependencyUnavailable(
                        "no Bedrock client available (pool busy)",
                        stage="bedrock") from exc
        try:
            yield client
        finally:
            self._free.put(client)

    def warm(self) -> bool:
        """Create one client eagerly to validate OIDC at startup. Returns success."""
        try:
            with self.acquire(timeout=5):
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Bedrock warm failed: %s", exc)
            return False

    def ping(self) -> bool:
        try:
            with self.acquire(timeout=2) as c:
                return c.ping()
        except Exception:  # noqa: BLE001
            return False


class Resources:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pg = PGPool(settings)                 # raises if PG unreachable at startup
        self.embed = EmbeddingClient(settings)
        self.bedrock = BedrockClientPool(settings)
        agents.tools.bind_pool(self.pg)            # tools use the shared pool
        log.info("resources initialized")

    def close(self) -> None:
        self.pg.close()

    def readiness(self) -> dict:
        pg_ok = self.pg.ping()
        embed_ok = self.embed.ping()
        bedrock_ok = self.bedrock.ping()
        checks = {"postgres": pg_ok, "embedding": embed_ok, "bedrock": bedrock_ok}
        if not pg_ok:
            status = "unavailable"           # PG is the only hard dependency
        elif not (embed_ok and bedrock_ok):
            status = "degraded"              # embedding down → sparse-only; bedrock warns
        else:
            status = "ready"
        return {"status": status, "checks": checks}
