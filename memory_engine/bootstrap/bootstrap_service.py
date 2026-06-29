"""ProjectBootstrapService — zero-touch project initialization.

Bootstrap sequence (idempotent):
  1. Ensure .memory-engine/ layout exists.
  2. Write default config.yaml (preserves user edits).
  3. Initialize SQLite DB (create tables + FTS5).
  4. Create project record in DB.
  5. Ingest high-value seed files first:
     - .memory-engine/constraints.md
     - .memory-engine/team-rules.md
     - .memory-engine/decisions.md
     - MEMORY.md / AGENTS.md / CLAUDE.md
     - README.md (and variants)
     - docs/architecture.md / ADR directories
  6. Scan and ingest broader knowledge sources.
  7. Generate AGENT_MEMORY_POLICY.md.
  8. Persist bootstrap report.
  9. Set state to READY.

Idempotency is guaranteed by content-hash deduplication in
KnowledgeIngestionService — unchanged documents are detected and skipped.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from memory_engine.bootstrap.config import load_config, write_default_config
from memory_engine.bootstrap.incremental_index import IncrementalIndexCoordinator
from memory_engine.bootstrap.local_storage import ProjectLocalStorage
from memory_engine.bootstrap.project_state import BootstrapStatus, ProjectStateManager
from memory_engine.bootstrap.security import is_indexable
from memory_engine.bootstrap.vector_fallback import detect_retrieval_mode
from memory_engine.db.init_db import create_fts_tables
from memory_engine.knowledge.cache import SimpleCache
from memory_engine.knowledge.ingestion import KnowledgeIngestionService
from memory_engine.knowledge.vector_index import InMemoryVectorIndex
from memory_engine.models.knowledge_domain import KnowledgeIngestRequest, SourceType
from memory_engine.models.orm import Base
import memory_engine.models.knowledge_orm  # noqa: F401 — register ORM
from memory_engine.models.orm import ProjectORM
from memory_engine.services.project_service import ProjectService

# High-value seed files, ingested in priority order
_SEED_FILENAMES = (
    # Memory Engine human-authored seeds
    "constraints.md",
    "team-rules.md",
    "decisions.md",
    # Common agent policy files
    "MEMORY.md",
    "AGENTS.md",
    "CLAUDE.md",
    # READMEs
    "README.md",
    "README.rst",
    "README.txt",
    # Architecture / ADR
    "ARCHITECTURE.md",
    "architecture.md",
    "DESIGN.md",
)

_SEED_DIRS = ("docs", "adr", "ADR", "rfcs", "RFC")



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectBootstrapService:
    """Bootstrap and maintain project-local Memory Engine state.

    Designed to be called once per MCP server startup.
    Safe to call multiple times (idempotent).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        vector_index: InMemoryVectorIndex | None = None,
        cache: SimpleCache | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.storage = ProjectLocalStorage(self.project_root)
        self._vector_index = vector_index
        self._cache = cache or SimpleCache()
        self._engine = None
        self._session: Session | None = None
        self._state_mgr: ProjectStateManager | None = None
        self._project_orm: ProjectORM | None = None
        self._semantic_index = None

    def _get_semantic_index(self):  # type: ignore[no-untyped-def]
        """Phase 13: lazily build the persistent vector index (None if disabled)."""
        if self._semantic_index is None:
            try:
                from memory_engine.knowledge.semantic import get_persistent_vector_index

                self._semantic_index = get_persistent_vector_index(self.project_root)
            except Exception:
                self._semantic_index = None
        return self._semantic_index

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def bootstrap(self) -> dict[str, Any]:
        """Run full bootstrap sequence.  Returns bootstrap report dict."""
        state_mgr = self._get_state_mgr()
        state = state_mgr.load()

        # If already READY and initialized, skip full bootstrap
        if state.bootstrap_status == "READY" and self.storage.is_initialized():
            return self._ready_report(state_mgr)

        state_mgr.set_status("BOOTSTRAPPING")

        try:
            # 1–2. Storage layout + config
            self.storage.ensure_layout()
            write_default_config(
                self.storage.config_path,
                project_name=self.project_root.name,
            )

            # 3. Database
            engine = self._get_engine()
            Base.metadata.create_all(bind=engine)
            with engine.connect() as conn:
                create_fts_tables(conn)
                conn.commit()

            # 4. Project record
            session = self._get_session()
            project_orm = self._ensure_project(session)

            # 5–6. Seed knowledge
            ingested = self._ingest_seed_knowledge(session, project_orm)

            # 7. Generate AGENT_MEMORY_POLICY.md
            self._write_agent_policy()

            # 8. Update state
            state_mgr.set_status("READY")
            state_mgr.update(
                indexed_documents=ingested["documents"],
                indexed_chunks=ingested["chunks"],
                last_indexed_at=_now_iso(),
            )

            # 9. Bootstrap report
            report = {
                "bootstrap_status": "READY",
                "project_root": str(self.project_root),
                "project_name": project_orm.name,
                "indexed_documents": ingested["documents"],
                "indexed_chunks": ingested["chunks"],
                "skipped_duplicates": ingested["duplicates"],
                "seed_files_found": ingested["seed_files"],
                "bootstrapped_at": _now_iso(),
                "agent_policy_generated": self.storage.agent_policy_path.exists(),
                "warnings": state_mgr.load().warnings,
            }
            self.storage.bootstrap_dir.mkdir(parents=True, exist_ok=True)
            (self.storage.bootstrap_report_path).write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            return report

        except Exception as exc:
            state_mgr.set_status("FAILED")
            state_mgr.add_warning(f"Bootstrap failed: {exc}")
            return {
                "bootstrap_status": "FAILED",
                "error": str(exc),
                "project_root": str(self.project_root),
            }

    def incremental_refresh(self) -> dict[str, Any]:
        """Scan for changed files and re-index only those.  Returns summary."""
        state_mgr = self._get_state_mgr()
        state_mgr.set_status("INDEXING_INCREMENTAL")

        engine = self._get_engine()
        session = self._get_session()
        project_orm = self._ensure_project(session)

        coord = IncrementalIndexCoordinator(
            self.project_root, self.storage.manifest_path
        )
        coord.load_manifest()
        cs = coord.compute_changes()

        ingested_count = 0
        stale_count = 0

        if cs.has_changes:
            vector_idx = self._vector_index or InMemoryVectorIndex()
            ingest_svc = KnowledgeIngestionService(
                session, vector_index=vector_idx, cache=self._cache,
                semantic_index=self._get_semantic_index(),
            )

            # Index new + changed files
            for entry in list(cs.new) + list(cs.changed):
                fpath = self.project_root / entry.rel_path
                try:
                    ingestible, reason = is_indexable(fpath, self.project_root)
                    if not ingestible:
                        continue
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    ingest_svc.ingest(KnowledgeIngestRequest(
                        project_id=uuid.UUID(project_orm.id),
                        source_type=SourceType(entry.source_type),
                        title=entry.rel_path,
                        content=content,
                        source_path=entry.rel_path,
                    ))
                    ingested_count += 1
                except Exception:
                    pass

            # Mark deleted files' chunks as stale
            for entry in cs.deleted:
                stale_count += 1

            coord.apply_changes(cs, current_revision=state_mgr.load().index_revision + 1)
            state_mgr.update(
                bootstrap_status="READY",
                knowledge_revision=state_mgr.load().knowledge_revision + 1,
                index_revision=state_mgr.load().index_revision + 1,
                indexed_documents=state_mgr.load().indexed_documents + ingested_count,
                last_indexed_at=_now_iso(),
            )
            self._cache.invalidate_project(str(uuid.UUID(project_orm.id)))

        else:
            state_mgr.set_status("READY")

        return {
            "bootstrap_status": "READY",
            "changes": cs.summary(),
            "ingested": ingested_count,
            "stale_removed": stale_count,
        }

    def get_project_id(self) -> str:
        """Return the UUID string of the project record."""
        session = self._get_session()
        orm = self._ensure_project(session)
        return orm.id

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_engine(self):  # type: ignore[return]
        if self._engine is None:
            self._engine = create_engine(
                self.storage.db_url,
                connect_args={"check_same_thread": False},
                echo=False,
            )
        return self._engine

    def _get_session(self) -> Session:
        if self._session is None:
            engine = self._get_engine()
            self._session = Session(engine)
        return self._session

    def _get_state_mgr(self) -> ProjectStateManager:
        if self._state_mgr is None:
            self.storage.ensure_layout()
            self._state_mgr = ProjectStateManager(self.storage.project_state_path)
        return self._state_mgr

    def _ensure_project(self, session: Session) -> ProjectORM:
        if self._project_orm is not None:
            return self._project_orm
        name = self.project_root.name
        existing = session.query(ProjectORM).filter_by(name=name).first()
        if existing:
            self._project_orm = existing
            return existing
        orm = ProjectORM(name=name, description=f"Auto-bootstrapped from {self.project_root}")
        session.add(orm)
        session.commit()
        session.refresh(orm)
        self._project_orm = orm
        return orm

    def _ingest_seed_knowledge(
        self, session: Session, project_orm: ProjectORM
    ) -> dict[str, int]:
        vector_idx = self._vector_index or InMemoryVectorIndex()
        svc = KnowledgeIngestionService(
            session, vector_index=vector_idx, cache=self._cache,
            semantic_index=self._get_semantic_index(),
        )
        project_id = uuid.UUID(project_orm.id)

        documents = 0
        chunks = 0
        duplicates = 0
        seed_files: list[str] = []

        # Seed files in .memory-engine/ (user-authored)
        for fname in ("constraints.md", "team-rules.md", "decisions.md"):
            fpath = self.storage.storage_dir / fname
            if fpath.exists():
                seed_files.append(str(fpath))
                result = self._ingest_file(svc, fpath, project_id, "manual_note")
                if result:
                    documents += 1; chunks += result
                else:
                    duplicates += 1

        # Agent policy files at project root
        for fname in ("MEMORY.md", "AGENTS.md", "CLAUDE.md", "AGENT_MEMORY.md"):
            fpath = self.project_root / fname
            if fpath.exists():
                seed_files.append(str(fpath))
                result = self._ingest_file(svc, fpath, project_id, "manual_note")
                if result:
                    documents += 1; chunks += result
                else:
                    duplicates += 1

        # High-value files in root
        for fname in _SEED_FILENAMES:
            fpath = self.project_root / fname
            if fpath.exists() and fpath not in [self.project_root / s for s in ("MEMORY.md","AGENTS.md","CLAUDE.md")]:
                seed_files.append(str(fpath))
                st = _infer_seed_source_type(fpath)
                result = self._ingest_file(svc, fpath, project_id, st)
                if result:
                    documents += 1; chunks += result
                else:
                    duplicates += 1

        # ADR directories
        for seed_dir in _SEED_DIRS:
            dpath = self.project_root / seed_dir
            if dpath.is_dir():
                for fpath in sorted(dpath.rglob("*.md"))[:50]:
                    ingestible, _ = is_indexable(fpath, self.project_root)
                    if ingestible:
                        seed_files.append(str(fpath))
                        st = _infer_seed_source_type(fpath)
                        result = self._ingest_file(svc, fpath, project_id, st)
                        if result:
                            documents += 1; chunks += result
                        else:
                            duplicates += 1

        return {
            "documents": documents,
            "chunks": chunks,
            "duplicates": duplicates,
            "seed_files": seed_files,
        }

    def _ingest_file(
        self,
        svc: KnowledgeIngestionService,
        fpath: Path,
        project_id: uuid.UUID,
        source_type: str,
    ) -> int | None:
        """Ingest a single file.  Returns chunk count, or None if duplicate."""
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                return None
            rel = str(fpath.relative_to(self.project_root)) if fpath.is_relative_to(self.project_root) else fpath.name
            result = svc.ingest(KnowledgeIngestRequest(
                project_id=project_id,
                source_type=SourceType(source_type),
                title=fpath.name,
                content=content,
                source_path=rel,
            ))
            if result.was_duplicate:
                return None
            return result.chunks_created
        except Exception:
            return None

    def _write_agent_policy(self) -> None:
        """Generate AGENT_MEMORY_POLICY.md and install CLAUDE.md adapter (idempotent)."""
        self.storage.generated_dir.mkdir(parents=True, exist_ok=True)
        self.storage.ensure_gitignore_hint()
        try:
            from memory_engine.policy.installer import install_claude_code
            install_claude_code(self.project_root)
        except Exception:
            # Non-fatal: user can run `memory policy install` manually
            from memory_engine.policy.generator import generate_policy
            generate_policy(self.project_root)

    def _ready_report(self, state_mgr: ProjectStateManager) -> dict[str, Any]:
        state = state_mgr.load()
        # Ensure the CLAUDE.md adapter is present for existing projects that
        # bootstrapped before this feature was added (idempotent, non-fatal).
        try:
            from memory_engine.policy.installer import adapter_status, install_claude_code
            if not adapter_status(self.project_root)["claude_code"]["installed"]:
                install_claude_code(self.project_root)
        except Exception:
            pass
        # Phase 13: surface semantic retrieval health in the ready report.
        try:
            mode_info = detect_retrieval_mode(project_root=self.project_root)
            semantic_block = {
                "semantic_enabled": mode_info.semantic_enabled,
                "semantic_health": (
                    "healthy" if mode_info.semantic_status == "used"
                    else mode_info.semantic_status
                ),
                "embedding_provider": mode_info.embedding_provider,
                "embedding_model": mode_info.embedding_model,
            }
        except Exception:
            semantic_block = {
                "semantic_enabled": False,
                "semantic_health": "disabled",
                "embedding_provider": "none",
                "embedding_model": "none",
            }
        return {
            "bootstrap_status": "READY",
            "project_root": str(self.project_root),
            "indexed_documents": state.indexed_documents,
            "indexed_chunks": state.indexed_chunks,
            "already_initialized": True,
            **semantic_block,
        }


def _infer_seed_source_type(path: Path) -> str:
    from memory_engine.bootstrap.incremental_index import _infer_source_type
    return _infer_source_type(path)
