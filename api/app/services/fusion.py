"""Shared hybrid-fusion primitives: enriched-BM25 sparse literal + z-score fuse.

The retrieval research's best method:
    score = W_DENSE * zscore(dense)  +  W_SPARSE * zscore(sparse)

Used by ticket retrieval (``era_corpus``) AND schema-catalog semantic search
(``schema_tables`` / ``schema_columns``) so all three share ONE fusion definition.
"""
from __future__ import annotations

import re

import numpy as np

TOKEN_RE = re.compile(r"[a-z0-9]+")
W_DENSE, W_SPARSE = 0.3, 0.7
POOL = 200                     # candidate pool per signal before fusing


def sparse_literal(text: str, vocab: dict, dim: int) -> str | None:
    """Query TF as a pgvector ``sparsevec`` literal over ``vocab``; None if no token hits."""
    counts: dict[int, int] = {}
    for t in TOKEN_RE.findall(text.lower()):
        j = vocab.get(t)
        if j:
            counts[j] = counts.get(j, 0) + 1
    if not counts:
        return None
    body = ",".join(f"{i}:{v}" for i, v in sorted(counts.items()))
    return "{" + body + "}/" + str(dim)


def zscore_fuse(dense: dict, sparse: dict, *, w_dense=W_DENSE, w_sparse=W_SPARSE):
    """z-score fuse over the union of candidate pools → [{id,fused,dense,sparse}] best-first."""
    def stats(d):
        if not d:
            return 0.0, 1.0
        v = np.array(list(d.values()))
        return float(v.mean()), float(v.std() or 1.0)
    dmu, dsd = stats(dense)
    smu, ssd = stats(sparse)
    out = []
    for c in set(dense) | set(sparse):
        zd = (dense[c] - dmu) / dsd if c in dense else -1.0
        zs = (sparse[c] - smu) / ssd if c in sparse else -1.0
        out.append({"id": c, "fused": w_dense * zd + w_sparse * zs,
                    "dense": dense.get(c), "sparse": sparse.get(c)})
    out.sort(key=lambda x: -x["fused"])
    return out
