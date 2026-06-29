"""VectorFallbackManager — detect vector backend availability and select retrieval mode.

Retrieval modes:
  hybrid                       — FTS5 + persistent sqlite-vec semantic search (Phase 13)
  hybrid_lexical_vector        — legacy alias retained for backward compatibility
  lexical_structured_fallback  — FTS5 + module/symbol/tree scoring only, no dense vector

Phase 13:
  When semantic retrieval is enabled in config AND both sqlite-vec and a
  configured embedding provider are available, the mode becomes "hybrid" with a
  persistent "sqlite_vec" backend. Otherwise the engine stays in the existing
  "lexical_structured_fallback" mode with an "ephemeral" in-memory index, and a
  diagnostic warning explains why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

RetrievalMode = Literal[
    "hybrid",
    "hybrid_lexical_vector",
    "lexical_structured_fallback",
]
VectorBackendStatus = Literal[
    "sqlite_vec",
    "qdrant_embedded",
    "healthy",
    "ephemeral",
    "unavailable",
]


@dataclass
class RetrievalModeInfo:
    mode: RetrievalMode
    vector_backend: VectorBackendStatus
    warnings: list[str] = field(default_factory=list)
    # Phase 13 diagnostics (additive, backward-compatible)
    semantic_enabled: bool = False
    embedding_provider: str = "none"
    embedding_model: str = "none"
    semantic_status: str = "disabled"   # disabled | unavailable | used | degraded


def detect_retrieval_mode(
    project_root: Path | None = None,
    config=None,  # type: ignore[no-untyped-def]
) -> RetrievalModeInfo:
    """Determine which retrieval mode is available in the current environment.

    Phase 13 logic:
      - If semantic retrieval is disabled in config → lexical fallback (unchanged).
      - If enabled: probe sqlite_vec importability + provider.is_available().
        Both OK → hybrid mode with sqlite_vec backend.
        Otherwise → lexical fallback with a diagnostic warning.
    """
    if config is None:
        try:
            from memory_engine.knowledge.semantic import get_semantic_config

            config = get_semantic_config(project_root)
        except Exception:
            config = None

    semantic_enabled = bool(getattr(config, "enabled", False))
    provider_name = getattr(config, "provider", "none")
    model_name = getattr(config, "model", "none")

    if not semantic_enabled:
        return RetrievalModeInfo(
            mode="lexical_structured_fallback",
            vector_backend="ephemeral",
            semantic_enabled=False,
            embedding_provider="none",
            embedding_model="none",
            semantic_status="disabled",
            warnings=[
                "Semantic vector retrieval is disabled. "
                "Results use lexical, module-path, symbol, and memory-tree ranking. "
                "Enable with: memory semantic status --enable --project-root <your-project> "
                "(after installing memory-engine[semantic-transformers])."
            ],
        )

    # Semantic is requested — probe backend + provider.
    from memory_engine.knowledge.embedding import build_provider
    from memory_engine.knowledge.sqlite_vec_index import sqlite_vec_available

    backend_ok = sqlite_vec_available()
    provider = build_provider(config)
    provider_available = provider.is_available()

    if backend_ok and provider_available:
        return RetrievalModeInfo(
            mode="hybrid",
            vector_backend="sqlite_vec",
            semantic_enabled=True,
            embedding_provider=provider.provider_name,
            embedding_model=getattr(provider, "model_name", model_name),
            semantic_status="used",
            warnings=[],
        )

    warnings: list[str] = []
    if not backend_ok:
        warnings.append(
            "Semantic retrieval requested but sqlite-vec is not installed. "
            "Install memory-engine[semantic-sqlite]. Falling back to lexical."
        )
    if not provider_available:
        warnings.append(
            f"Embedding provider '{provider_name}' is unavailable "
            "(not installed or service unreachable). Falling back to lexical."
        )
    return RetrievalModeInfo(
        mode="lexical_structured_fallback",
        vector_backend="unavailable",
        semantic_enabled=True,
        embedding_provider=provider_name,
        embedding_model=model_name,
        semantic_status="unavailable",
        warnings=warnings,
    )


def degraded_response_metadata(mode_info: RetrievalModeInfo) -> dict:  # type: ignore[type-arg]
    """Build the degraded-mode metadata block to include in tool responses."""
    return {
        "retrieval_mode": mode_info.mode,
        "vector_backend": mode_info.vector_backend,
        "warnings": mode_info.warnings,
        "semantic_enabled": mode_info.semantic_enabled,
        "embedding_provider": mode_info.embedding_provider,
        "embedding_model": mode_info.embedding_model,
        "semantic_status": mode_info.semantic_status,
    }
