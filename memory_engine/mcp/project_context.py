"""ProjectContext — lazy-initialized per-project runtime state.

Holds:
  - Project-local SQLAlchemy engine + session
  - InMemoryVectorIndex (ephemeral; future: persistent backend)
  - SimpleCache (TTL-based, in-process)
  - ProjectBootstrapService
  - ProjectStateManager
  - RetrievalModeInfo

One ProjectContext per process per project root.
Thread-safe bootstrapping via a simple flag guard.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService
from memory_engine.bootstrap.local_storage import ProjectLocalStorage
from memory_engine.bootstrap.project_state import ProjectStateManager
from memory_engine.bootstrap.vector_fallback import RetrievalModeInfo, detect_retrieval_mode
from memory_engine.knowledge.cache import SimpleCache
from memory_engine.knowledge.vector_index import InMemoryVectorIndex
from memory_engine.models.orm import Base
import memory_engine.models.knowledge_orm  # noqa: F401 — register ORM
from memory_engine.db.init_db import create_fts_tables, apply_schema_migrations
from memory_engine.runtime.git import GitContext, GitContextResolver


class ProjectContext:
    """All runtime objects needed to serve one target project."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.storage = ProjectLocalStorage(self.project_root)
        self._bootstrapped = False
        self._engine = None
        self._session_factory = None
        self._vector_index: InMemoryVectorIndex | None = None
        self._cache: SimpleCache | None = None
        self._mode_info: RetrievalModeInfo | None = None
        self._project_id: str | None = None
        self._git_context: GitContext | None = None   # Phase 9: cached per-call

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def ensure_bootstrapped(self) -> dict:  # type: ignore[type-arg]
        """Bootstrap the project if not already done.  Returns bootstrap report."""
        if self._bootstrapped:
            return {"bootstrap_status": "READY", "already_initialized": True}

        svc = self._make_bootstrap_service()
        report = svc.bootstrap()
        self._project_id = svc.get_project_id()
        self._bootstrapped = report.get("bootstrap_status") in ("READY", "DEGRADED")
        return report

    def incremental_refresh(self) -> dict:  # type: ignore[type-arg]
        """Trigger incremental re-index scan."""
        svc = self._make_bootstrap_service()
        return svc.incremental_refresh()

    # ------------------------------------------------------------------
    # Resource accessors
    # ------------------------------------------------------------------

    def get_engine(self):  # type: ignore[return]
        if self._engine is None:
            self.storage.ensure_layout()
            self._engine = create_engine(
                self.storage.db_url,
                connect_args={"check_same_thread": False},
                echo=False,
            )
            Base.metadata.create_all(bind=self._engine)
            with self._engine.connect() as conn:
                create_fts_tables(conn)
                apply_schema_migrations(conn)  # Phase 9: idempotent ADD COLUMN
                conn.commit()
        return self._engine

    def get_session(self) -> Session:
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.get_engine())
        return self._session_factory()

    def get_vector_index(self) -> InMemoryVectorIndex:
        if self._vector_index is None:
            self._vector_index = InMemoryVectorIndex()
        return self._vector_index

    def get_cache(self) -> SimpleCache:
        if self._cache is None:
            self._cache = SimpleCache(ttl_seconds=300)
        return self._cache

    def get_mode_info(self) -> RetrievalModeInfo:
        if self._mode_info is None:
            self._mode_info = detect_retrieval_mode()
        return self._mode_info

    def get_project_id(self) -> str:
        if self._project_id is None:
            # Try loading from DB
            session = self.get_session()
            try:
                from memory_engine.models.orm import ProjectORM
                name = self.project_root.name
                orm = session.query(ProjectORM).filter_by(name=name).first()
                if orm:
                    self._project_id = orm.id
                else:
                    self._project_id = str(uuid.uuid4())
            finally:
                session.close()
        return self._project_id  # type: ignore[return-value]

    def get_state_manager(self) -> ProjectStateManager:
        self.storage.ensure_layout()
        return ProjectStateManager(self.storage.project_state_path)

    def get_git_context(self, *, refresh: bool = False) -> GitContext:
        """Return (and optionally refresh) the Git context for this project root.

        The context is re-resolved every call when refresh=True,
        otherwise cached for the lifetime of the process.
        """
        if self._git_context is None or refresh:
            resolver = GitContextResolver(self.project_root)
            self._git_context = resolver.resolve()
        return self._git_context

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_bootstrap_service(self) -> ProjectBootstrapService:
        return ProjectBootstrapService(
            self.project_root,
            vector_index=self.get_vector_index(),
            cache=self.get_cache(),
        )


# ---------------------------------------------------------------------------
# Module-level registry: one context per project root
# ---------------------------------------------------------------------------

_registry: dict[str, ProjectContext] = {}


def get_project_context(project_root: Path) -> ProjectContext:
    """Return (and lazily create) the ProjectContext for a given project root."""
    key = str(project_root.resolve())
    if key not in _registry:
        _registry[key] = ProjectContext(project_root)
    return _registry[key]


def clear_registry() -> None:
    """Clear all cached contexts (use in tests)."""
    _registry.clear()
