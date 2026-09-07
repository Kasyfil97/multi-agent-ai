"""bge-m3 embedding client (OpenAI-compatible) for the dense retrieval signal.

Single-query encoder with timeout + light retry. Raises ``DependencyUnavailable`` on
failure so retrieval can fall back to sparse-only BM25 (graceful degradation).
"""
from __future__ import annotations

import time

import numpy as np
import requests

from ..config import Settings
from ..errors import DependencyUnavailable
from ..logging_config import get_logger

log = get_logger("clients.embeddings")


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.url = settings.embed_url
        self.token = settings.embed_token
        self.model = settings.embed_model
        self.timeout = settings.embed_timeout

    def embed_query(self, text: str, retries: int = 1) -> np.ndarray:
        """Return a normalized float32 vector. Raises DependencyUnavailable on error."""
        if not self.url:
            raise DependencyUnavailable("EMBED_URL not configured", stage="embedding")
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        payload = {"model": self.model, "input": [text]}
        last = None
        for attempt in range(retries + 1):
            try:
                r = requests.post(self.url, headers=headers, json=payload,
                                  timeout=self.timeout)
                r.raise_for_status()
                v = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
                n = np.linalg.norm(v) or 1.0
                return v / n
            except requests.RequestException as exc:
                last = exc
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
        raise DependencyUnavailable(f"embedding service unreachable: {last}",
                                    stage="embedding")

    def ping(self) -> bool:
        try:
            self.embed_query("ping", retries=0)
            return True
        except Exception:  # noqa: BLE001
            return False
