"""KnowledgeIngestionService — ingest documents into the knowledge base.

Flow:
  1. Redact secrets from raw content.
  2. Compute content hash.
  3. Check for existing document with same project_id + source_path + hash.
     If unchanged → skip (idempotent).
     If changed → mark old chunks stale, create new document record.
  4. Chunk content according to source type.
  5. Persist KnowledgeDocumentORM + KnowledgeChunkORM rows.
  6. Insert each chunk into FTS5 index.
  7. Insert each chunk into VectorIndex (generates embeddings synchronously
     here; production would defer to a job queue).
  8. Create a KnowledgeIndexJobORM record (status=done for synchronous path).
  9. Update document status → indexed.
  10. Invalidate project cache.

Memory and knowledge are NEVER mixed:
  KnowledgeChunks contain raw source content.
  MemoryNodes contain curated, compressed understanding.
  This service only touches the knowledge_* tables.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from memory_engine.knowledge.cache import SimpleCache, get_global_cache
from memory_engine.knowledge.chunkers import chunk_content
from memory_engine.knowledge.fts_index import fts_delete, fts_insert
from memory_engine.knowledge.redaction import redact
from memory_engine.knowledge.vector_index import InMemoryVectorIndex, KnowledgeVectorIndex
from memory_engine.models.knowledge_domain import (
    KnowledgeIngestRequest,
    KnowledgeIngestResult,
)
from memory_engine.models.knowledge_orm import (
    KnowledgeChunkORM,
    KnowledgeDocumentORM,
    KnowledgeIndexJobORM,
)


def _sha256(text_: str) -> str:
    return hashlib.sha256(text_.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeIngestionService:
    """Ingest raw documents into the knowledge base.

    Designed to be instantiated per-request (like all other services).
    The vector_index is typically a module-level singleton (InMemoryVectorIndex
    or QdrantVectorIndex) shared across requests.
    """

    def __init__(
        self,
        session: Session,
        vector_index: KnowledgeVectorIndex | None = None,
        cache: SimpleCache | None = None,
    ) -> None:
        self._session = session
        self._vector_index: KnowledgeVectorIndex = vector_index or _get_default_vector_index()
        self._cache = cache or get_global_cache()

    def ingest(self, req: KnowledgeIngestRequest) -> KnowledgeIngestResult:
        """Ingest a single document.  Returns immediately (synchronous path)."""
        project_id_str = str(req.project_id)

        # 1. Redact
        redacted_content, redaction_count = redact(req.content)

        # 2. Hash
        content_hash = _sha256(redacted_content)

        # 3. Check for existing document at same source_path with same hash
        existing = None
        if req.source_path:
            existing = (
                self._session.query(KnowledgeDocumentORM)
                .filter_by(project_id=project_id_str, source_path=req.source_path)
                .order_by(KnowledgeDocumentORM.created_at.desc())
                .first()
            )

        if existing and existing.content_hash == content_hash:
            # Unchanged — skip re-indexing
            chunk_count = (
                self._session.query(KnowledgeChunkORM)
                .filter_by(document_id=existing.document_id)
                .count()
            )
            return KnowledgeIngestResult(
                document_id=uuid.UUID(existing.document_id),
                chunks_created=chunk_count,
                was_duplicate=True,
                was_updated=False,
                redaction_count=redaction_count,
            )

        was_updated = existing is not None

        # If updating — mark old chunks as stale (keep for audit)
        if was_updated and existing is not None:
            self._session.query(KnowledgeChunkORM).filter_by(
                document_id=existing.document_id
            ).update({"index_status": "stale"})
            existing.status = "outdated"
            self._session.flush()

        # 4–5. Create document record
        doc = KnowledgeDocumentORM(
            project_id=project_id_str,
            source_type=req.source_type.value,
            title=req.title,
            source_path=req.source_path,
            content_hash=content_hash,
            version_ref=req.version_ref,
            branch_name=req.branch_name,
            status="pending",
            tags=req.tags,
        )
        self._session.add(doc)
        self._session.flush()  # populate doc.document_id

        # 6. Chunk
        raw_chunks = chunk_content(
            redacted_content,
            source_type=req.source_type.value,
            source_path=req.source_path,
        )

        # 7. Persist chunks + index
        job = KnowledgeIndexJobORM(
            project_id=project_id_str,
            document_id=doc.document_id,
            status="running",
            chunks_total=len(raw_chunks),
            chunks_done=0,
        )
        self._session.add(job)
        self._session.flush()

        chunks_created = 0
        for raw in raw_chunks:
            chunk_content_str = raw.content.strip()
            if not chunk_content_str:
                continue
            chunk_hash = _sha256(chunk_content_str)
            chunk = KnowledgeChunkORM(
                document_id=doc.document_id,
                project_id=project_id_str,
                content=chunk_content_str,
                content_hash=chunk_hash,
                chunk_index=raw.chunk_index,
                token_count=raw.token_count,
                heading_path=raw.heading_path,
                module_paths=raw.module_paths,
                related_symbols=raw.related_symbols,
                tags=req.tags,
                language=raw.language,
                source_type=req.source_type.value,
                source_path=req.source_path,
                start_line=raw.start_line,
                end_line=raw.end_line,
                embedding_status="done",
                index_status="indexed",
            )
            self._session.add(chunk)
            self._session.flush()  # populate chunk.chunk_id

            # FTS5
            fts_insert(
                self._session,
                chunk_id=chunk.chunk_id,
                content=chunk_content_str,
                heading_path=raw.heading_path,
                symbols=raw.related_symbols,
                module_paths=raw.module_paths,
                tags=req.tags,
            )

            # Vector index
            self._vector_index.upsert(
                chunk_id=chunk.chunk_id,
                content=chunk_content_str,
                metadata={
                    "project_id": project_id_str,
                    "source_type": req.source_type.value,
                    "module_paths": raw.module_paths,
                    "related_symbols": raw.related_symbols,
                    "source_path": req.source_path or "",
                    "index_status": "indexed",
                    "version_ref": req.version_ref or "",
                    "tags": req.tags,
                },
            )
            chunks_created += 1
            job.chunks_done += 1

        # 8. Finalize
        doc.status = "indexed"
        job.status = "done"
        self._session.commit()

        # 9. Invalidate cache
        self._cache.invalidate_project(project_id_str)

        return KnowledgeIngestResult(
            document_id=uuid.UUID(doc.document_id),
            chunks_created=chunks_created,
            was_duplicate=False,
            was_updated=was_updated,
            redaction_count=redaction_count,
            index_job_id=uuid.UUID(job.job_id),
        )


# ---------------------------------------------------------------------------
# Module-level default vector index (shared singleton per process)
# ---------------------------------------------------------------------------

_default_vector_index: InMemoryVectorIndex | None = None


def _get_default_vector_index() -> InMemoryVectorIndex:
    global _default_vector_index
    if _default_vector_index is None:
        _default_vector_index = InMemoryVectorIndex()
    return _default_vector_index


def get_shared_vector_index() -> InMemoryVectorIndex:
    """Return the module-level shared vector index."""
    return _get_default_vector_index()


def reset_shared_vector_index() -> None:
    """Reset the shared vector index (use in tests)."""
    global _default_vector_index
    _default_vector_index = InMemoryVectorIndex()
