"""VectorFallbackManager — detect vector backend availability and select retrieval mode.

Retrieval modes:
  hybrid_lexical_vector  — FTS5 + real vector search (Qdrant or InMemory with real embeddings)
  lexical_structured_fallback — FTS5 + module/symbol/tree scoring only, no dense vector

The InMemoryVectorIndex is always available as a same-process fallback,
but it is NOT persistent across process restarts.  We report it as
"ephemeral" and enter DEGRADED mode for the vector component.

A future QdrantVectorIndex with a healthy local database would be "persistent".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RetrievalMode = Literal["hybrid_lexical_vector", "lexical_structured_fallback"]
VectorBackendStatus = Literal["healthy", "ephemeral", "unavailable"]


@dataclass
class RetrievalModeInfo:
    mode: RetrievalMode
    vector_backend: VectorBackendStatus
    warnings: list[str] = field(default_factory=list)


def detect_retrieval_mode() -> RetrievalModeInfo:
    """Determine which retrieval mode is available in the current environment.

    Current logic:
      - If qdrant-client is installed and a local collection is healthy → hybrid.
      - Otherwise → lexical_structured_fallback with ephemeral InMemoryVectorIndex.

    Future persistent vector backends (Qdrant local, ChromaDB, etc.) would be
    detected and reported here via the VectorIndex Protocol.
    """
    # Try Qdrant
    try:
        import qdrant_client  # noqa: F401
        # TODO: probe a local collection here
        # For now, even if the import works, we don't have a healthy collection
        # unless QdrantVectorIndex has been configured and its DB file exists.
        # Leave as fallback until Phase 8 adds persistent Qdrant support.
    except ImportError:
        pass

    # Default: lexical structured fallback
    return RetrievalModeInfo(
        mode="lexical_structured_fallback",
        vector_backend="ephemeral",
        warnings=[
            "Semantic vector retrieval is unavailable. "
            "Results use lexical, module-path, symbol, and memory-tree ranking. "
            "Install qdrant-client and configure a local collection for full vector search."
        ],
    )


def degraded_response_metadata(mode_info: RetrievalModeInfo) -> dict:  # type: ignore[type-arg]
    """Build the degraded-mode metadata block to include in tool responses."""
    return {
        "retrieval_mode": mode_info.mode,
        "vector_backend": mode_info.vector_backend,
        "warnings": mode_info.warnings,
    }
