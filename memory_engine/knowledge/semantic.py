"""Semantic retrieval wiring — Phase 13.

Single place that turns configuration into a live persistent vector index.

  get_semantic_config()              → SemanticRetrievalSettings
  build_persistent_index(root, cfg)  → SqliteVecIndex | None
  get_persistent_vector_index(...)   → cached SqliteVecIndex per project root

Returns None whenever semantic retrieval is disabled, sqlite-vec is missing, or
no embedding provider is available — callers then keep using lexical retrieval.
"""

from __future__ import annotations

import os
from pathlib import Path

from memory_engine.bootstrap.local_storage import ProjectLocalStorage
from memory_engine.knowledge.embedding import (
    EmbeddingProvider,
    NoEmbeddingProvider,
    build_provider,
)


def get_semantic_config(project_root: Path | None = None):  # type: ignore[no-untyped-def]
    """Return the active SemanticRetrievalSettings.

    Priority (highest to lowest):
      1. MEMORY_ENGINE_* env vars
      2. .memory-engine/config.yaml `semantic:` section
      3. built-in defaults (enabled=False)
    """
    from memory_engine.config import SemanticRetrievalSettings

    cfg = SemanticRetrievalSettings.from_env()

    if project_root is not None:
        _apply_yaml_overrides(cfg, Path(project_root))

    return cfg


def _apply_yaml_overrides(cfg, project_root: Path) -> None:
    """Read config.yaml semantic section and fill gaps not covered by env vars."""
    yaml_path = project_root / ".memory-engine" / "config.yaml"
    if not yaml_path.exists():
        return
    try:
        import yaml

        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        sem = data.get("semantic", {})
        if not sem:
            return
        if os.environ.get("MEMORY_ENGINE_SEMANTIC_ENABLED") is None and "enabled" in sem:
            cfg.enabled = bool(sem["enabled"])
        if not os.environ.get("MEMORY_ENGINE_EMBEDDING_PROVIDER") and "provider" in sem:
            cfg.provider = str(sem["provider"])
        if not os.environ.get("MEMORY_ENGINE_EMBEDDING_MODEL") and "model" in sem:
            cfg.model = str(sem["model"])
        if not os.environ.get("MEMORY_ENGINE_VECTOR_BACKEND") and "backend" in sem:
            cfg.backend = str(sem["backend"])
    except Exception:
        pass


def build_provider_from_config(config=None) -> EmbeddingProvider:  # type: ignore[no-untyped-def]
    if config is None:
        config = get_semantic_config()
    return build_provider(config)


def build_persistent_index(project_root: Path, config=None):  # type: ignore[no-untyped-def]
    """Build a SqliteVecIndex for a project root, or None when unavailable."""
    if config is None:
        config = get_semantic_config(project_root)
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
