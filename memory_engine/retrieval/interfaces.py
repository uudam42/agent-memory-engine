"""Extension-point protocols for future vector / hybrid retrieval.

Stage 2 uses only deterministic local ranking (DeterministicRanker).
These interfaces define clean contracts for later plug-in of:
  - OpenAI / Voyage / local embedding providers
  - BM25 or TF-IDF lexical rankers
  - Hybrid re-rankers (RRF, linear combination)

Nothing in Stage 2 imports from this module except documentation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from memory_engine.models.domain import MemoryNode, ScoredMemory


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Encodes text into a dense vector.

    Stage 3+ will provide real implementations backed by embedding APIs.
    """

    def encode(self, text: str) -> list[float]:
        """Return a fixed-dimension embedding for the given text."""
        ...

    def similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """Return cosine similarity between two vectors in [0, 1]."""
        ...


@runtime_checkable
class LexicalRanker(Protocol):
    """Scores a corpus of memory nodes against a query using lexical signals."""

    def rank(self, query: str, nodes: list[MemoryNode]) -> list[ScoredMemory]:
        """Return nodes sorted by descending relevance score."""
        ...


@runtime_checkable
class HybridRanker(Protocol):
    """Combines dense-embedding scores and lexical scores.

    Reciprocal Rank Fusion or linear combination are both valid strategies.
    """

    def rank(self, query: str, nodes: list[MemoryNode]) -> list[ScoredMemory]:
        """Return nodes sorted by descending hybrid relevance score."""
        ...
