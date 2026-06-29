"""Semantic retrieval wiring — Phase 13.

Single place that turns configuration into a live persistent vector index.

  get_semantic_config()              → SemanticRetrievalSettings
  build_persistent_index(root, cfg)  → SqliteVecIndex | None
  get_persistent_vector_index(...)   → cached SqliteVecIndex per project root

Returns None whenever semantic retrieval is disabled, sqlite-vec is missing, or
no embedding provider is available — callers then keep using lexical retrieval.
"""

from __future__ import annotations

from pathlib import Path

from memory_engine.bootstrap.local_storage import ProjectLocalStorage
from memory_engine.knowledge.embedding import (
    EmbeddingProvider,
    NoEmbeddingProvider,
    build_provider,
)


def get_semantic_config():  # type: ignore[no-untyped-def]
    """Return the active SemanticRetrievalSettings (rebuilt from env each call)."""
    from memory_engine.config import SemanticRetrievalSettings

    return SemanticRetrievalSettings.from_env()


def build_provider_from_config(config=None) -> EmbeddingProvider:  # type: ignore[no-untyped-def]
    if config is None:
        config = get_semantic_config()
    return build_provider(config)


def build_persistent_index(project_root: Path, config=None):  # type: ignore[no-untyped-def]
    """Build a SqliteVecIndex for a project root, or None when unavailable."""
    if config is None:
        config = get_semantic_config()
    if not getattr(config, "enabled", False):
        return None

    from memory_engine.knowledge.sqlite_vec_index import (
        SqliteVecIndex,
        SqliteVecUnavailable,
        sqlite_vec_available,
    )

    if not sqlite_vec_available():
        return None

    provider = build_provider(config)
    if isinstance(provider, NoEmbeddingProvider) or not provider.is_available():
        return None

    storage = ProjectLocalStorage(project_root)
    storage.ensure_layout()

    # Dimension-safe migration: if the model's vector size changed since the last
    # run, clear stale vectors before reopening the index.
    try:
        import sqlite3

        from memory_engine.db.init_db import migrate_vector_tables

        dim = int(getattr(provider, "dimension", 0) or 0)
        if dim == 0 and provider.is_available():
            dim = len(provider.embed_query("dimension probe"))
        conn = sqlite3.connect(str(storage.vector_db_path))
        migrate_vector_tables(conn, dimension=dim)
        conn.close()
    except Exception:
        pass

    try:
        return SqliteVecIndex(storage.vector_db_path, provider)
    except SqliteVecUnavailable:
        return None


# Per-project cached persistent index (one connection per process per root).
_index_registry: dict[str, object] = {}


def get_persistent_vector_index(project_root: Path, config=None):  # type: ignore[no-untyped-def]
    """Return a cached SqliteVecIndex for project_root, or None when unavailable."""
    key = str(Path(project_root).resolve())
    if key in _index_registry:
        return _index_registry[key]
    index = build_persistent_index(Path(project_root), config)
    if index is not None:
        _index_registry[key] = index
    return index


def clear_index_registry() -> None:
    """Reset cached persistent indexes (use in tests)."""
    for idx in _index_registry.values():
        try:
            idx.close()  # type: ignore[attr-defined]
        except Exception:
            pass
    _index_registry.clear()
