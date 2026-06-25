"""KnowledgeVectorIndex — Protocol + two implementations.

Protocol:
  KnowledgeVectorIndex (runtime_checkable)
    upsert(chunk_id, content, metadata)
    search(query, project_id, limit, filter_metadata) → list[VectorHit]
    delete(chunk_id)
    clear_project(project_id)

Implementations:
  InMemoryVectorIndex  — deterministic bag-of-words cosine similarity.
                          No external deps.  Used by tests and local demo.
  QdrantVectorIndex    — Qdrant-Local backed (optional, requires qdrant-client).

The EmbeddingProvider protocol is also defined here.
Real embeddings improve semantic relevance but are NOT required.
The InMemoryVectorIndex generates its own TF-style vectors.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# EmbeddingProvider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Generate fixed-dimension dense embeddings for text."""

    def encode(self, text: str) -> list[float]: ...
    def similarity(self, a: list[float], b: list[float]) -> float: ...


# ---------------------------------------------------------------------------
# VectorHit result
# ---------------------------------------------------------------------------


@dataclass
class VectorHit:
    chunk_id: str
    score: float       # cosine similarity in [0, 1]
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# KnowledgeVectorIndex protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class KnowledgeVectorIndex(Protocol):
    def upsert(
        self,
        chunk_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None: ...

    def search(
        self,
        query: str,
        project_id: str,
        limit: int = 10,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[VectorHit]: ...

    def delete(self, chunk_id: str) -> None: ...

    def clear_project(self, project_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Deterministic bag-of-words helper
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "in", "it", "of", "to", "and", "or", "for",
    "be", "on", "at", "as", "by", "with", "that", "this", "was", "are",
    "from", "its", "i", "we", "they", "not", "no", "but", "if", "has",
    "have", "had", "do", "did", "will", "would", "should", "could",
    "been", "more", "into", "also", "when", "what", "which", "so",
})


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z][a-z0-9_]*", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _bow_vector(tokens: list[str]) -> dict[str, float]:
    freq: dict[str, float] = defaultdict(float)
    for t in tokens:
        freq[t] += 1.0
    total = sum(freq.values()) or 1.0
    return {t: c / total for t, c in freq.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(a.get(k, 0.0) * v for k, v in b.items())
    norm_a = math.sqrt(sum(v * v for v in a.values())) or 1e-9
    norm_b = math.sqrt(sum(v * v for v in b.values())) or 1e-9
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# InMemoryVectorIndex
# ---------------------------------------------------------------------------


class InMemoryVectorIndex:
    """Deterministic in-memory vector index using bag-of-words cosine.

    No external dependencies.  Suitable for tests and the local demo.
    When a real EmbeddingProvider is injected, it replaces the BoW vectors.
    """

    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self._provider = embedding_provider
        # chunk_id → (bow_vector, metadata)
        self._store: dict[str, tuple[dict[str, float], dict[str, Any]]] = {}

    def _vec(self, text: str) -> dict[str, float]:
        if self._provider is not None:
            vec_list = self._provider.encode(text)
            # Convert dense float list to dict for cosine computation
            return {str(i): v for i, v in enumerate(vec_list)}
        return _bow_vector(_tokenize(text))

    def upsert(
        self,
        chunk_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        self._store[chunk_id] = (self._vec(content), metadata)

    def search(
        self,
        query: str,
        project_id: str,
        limit: int = 10,
        filter_metadata: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        if not query.strip():
            return []
        q_vec = self._vec(query)
        results: list[VectorHit] = []

        for cid, (vec, meta) in self._store.items():
            if meta.get("project_id") != project_id:
                continue
            if meta.get("index_status") == "stale":
                continue
            # Apply optional metadata filters
            if filter_metadata:
                skip = False
                for k, v in filter_metadata.items():
                    if isinstance(v, list):
                        if not any(item in meta.get(k, []) for item in v):
                            skip = True
                            break
                    else:
                        if meta.get(k) != v:
                            skip = True
                            break
                if skip:
                    continue

            score = _cosine(q_vec, vec)
            results.append(VectorHit(chunk_id=cid, score=score, metadata=meta))

        results.sort(key=lambda h: h.score, reverse=True)
        return results[:limit]

    def delete(self, chunk_id: str) -> None:
        self._store.pop(chunk_id, None)

    def clear_project(self, project_id: str) -> None:
        to_del = [cid for cid, (_, m) in self._store.items()
                  if m.get("project_id") == project_id]
        for cid in to_del:
            del self._store[cid]


# ---------------------------------------------------------------------------
# Optional: QdrantVectorIndex
# ---------------------------------------------------------------------------

try:
    from qdrant_client import QdrantClient  # type: ignore[import-not-found]
    from qdrant_client.models import (  # type: ignore[import-not-found]
        Distance, FieldCondition, Filter, MatchAny, MatchValue,
        PointIdsList, PointStruct, VectorParams,
    )

    class QdrantVectorIndex:
        """Qdrant-local backed vector index.

        Requires:  pip install qdrant-client
        Creates an in-process local collection per project.
        """

        _COLLECTION = "knowledge_chunks"
        _DIM = 384  # all-MiniLM-L6-v2 dimensionality; override via embedding_provider

        def __init__(
            self,
            path: str = ":memory:",
            embedding_provider: EmbeddingProvider | None = None,
        ) -> None:
            self._client = QdrantClient(path=path)
            self._provider = embedding_provider
            self._ensure_collection()

        def _ensure_collection(self) -> None:
            cols = [c.name for c in self._client.get_collections().collections]
            if self._COLLECTION not in cols:
                self._client.create_collection(
                    self._COLLECTION,
                    vectors_config=VectorParams(size=self._DIM, distance=Distance.COSINE),
                )

        def _encode(self, text: str) -> list[float]:
            if self._provider:
                return self._provider.encode(text)
            # Deterministic fallback: project BoW onto fixed dim via hashing
            bow = _bow_vector(_tokenize(text))
            vec = [0.0] * self._DIM
            for token, weight in bow.items():
                idx = hash(token) % self._DIM
                vec[idx] += weight
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            return [v / norm for v in vec]

        def upsert(self, chunk_id: str, content: str, metadata: dict[str, Any]) -> None:
            vec = self._encode(content)
            self._client.upsert(self._COLLECTION, points=[
                PointStruct(id=_str_to_int(chunk_id), vector=vec, payload=metadata)
            ])

        def search(
            self,
            query: str,
            project_id: str,
            limit: int = 10,
            filter_metadata: dict[str, Any] | None = None,
        ) -> list[VectorHit]:
            q_vec = self._encode(query)
            must = [FieldCondition(key="project_id", match=MatchValue(value=project_id))]
            if filter_metadata:
                for k, v in filter_metadata.items():
                    if isinstance(v, list):
                        must.append(FieldCondition(key=k, match=MatchAny(any=v)))
                    else:
                        must.append(FieldCondition(key=k, match=MatchValue(value=v)))
            hits = self._client.search(
                self._COLLECTION,
                query_vector=q_vec,
                query_filter=Filter(must=must),
                limit=limit,
                with_payload=True,
            )
            return [VectorHit(
                chunk_id=str(h.id),
                score=float(h.score),
                metadata=dict(h.payload or {}),
            ) for h in hits]

        def delete(self, chunk_id: str) -> None:
            self._client.delete(
                self._COLLECTION,
                points_selector=PointIdsList(points=[_str_to_int(chunk_id)]),
            )

        def clear_project(self, project_id: str) -> None:
            from qdrant_client.models import FilterSelector  # type: ignore
            self._client.delete(
                self._COLLECTION,
                points_selector=FilterSelector(
                    filter=Filter(must=[
                        FieldCondition(key="project_id", match=MatchValue(value=project_id))
                    ])
                ),
            )

    def _str_to_int(s: str) -> int:
        """Map a UUID string to a uint64 for Qdrant point IDs."""
        import hashlib
        return int(hashlib.md5(s.encode()).hexdigest()[:16], 16)

except ImportError:
    # qdrant-client not installed — QdrantVectorIndex is unavailable
    pass
