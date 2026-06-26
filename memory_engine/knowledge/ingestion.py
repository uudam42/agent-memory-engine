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
from memory_engine.knowledge.fts_index import (
    fts_delete,
    fts_insert,
    paragraph_fts_delete,
    paragraph_fts_insert,
    proposition_fts_delete,
    proposition_fts_insert,
    summary_fts_delete,
    summary_fts_insert,
)
from memory_engine.knowledge.paragraph_segmenter import segment_paragraphs
from memory_engine.knowledge.proposition_extractor import extract_propositions
from memory_engine.knowledge.redaction import redact
from memory_engine.knowledge.summarizer import summarize_module, summarize_paragraphs
from memory_engine.knowledge.vector_index import InMemoryVectorIndex, KnowledgeVectorIndex
from memory_engine.models.knowledge_domain import (
    KnowledgeIngestRequest,
    KnowledgeIngestResult,
)
from memory_engine.models.knowledge_orm import (
    KnowledgeChunkORM,
    KnowledgeChunkSummaryORM,
    KnowledgeDocumentORM,
    KnowledgeIndexJobORM,
    KnowledgeParagraphORM,
    KnowledgePropositionORM,
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

        # If updating — mark old chunks + Phase 10 records as stale (keep for audit)
        if was_updated and existing is not None:
            self._session.query(KnowledgeChunkORM).filter_by(
                document_id=existing.document_id
            ).update({"index_status": "stale"})
            self._session.query(KnowledgeParagraphORM).filter_by(
                document_id=existing.document_id
            ).update({"is_stale": True})
            self._session.query(KnowledgePropositionORM).filter_by(
                document_id=existing.document_id
            ).update({"is_stale": True})
            self._session.query(KnowledgeChunkSummaryORM).filter_by(
                document_id=existing.document_id
            ).update({"is_stale": True})
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

        # Phase 10: multi-granularity write
        self._ingest_multigranular(
            doc_id=doc.document_id,
            project_id_str=project_id_str,
            redacted_content=redacted_content,
            source_type=req.source_type.value,
            source_path=req.source_path,
            branch_name=req.branch_name,
            branch_scope=None,
            source_revision=req.version_ref,
            commit_sha=None,
        )

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


    def _ingest_multigranular(
        self,
        doc_id: str,
        project_id_str: str,
        redacted_content: str,
        source_type: str,
        source_path: str | None,
        branch_name: str | None,
        branch_scope: str | None,
        source_revision: str | None,
        commit_sha: str | None,
    ) -> None:
        """Write paragraphs, propositions, and summaries for a document."""
        import hashlib as _hl

        def _h(s: str) -> str:
            return _hl.sha256(s.encode()).hexdigest()

        # --- Paragraphs ---
        raw_paras = segment_paragraphs(
            redacted_content, source_type=source_type, source_path=source_path
        )
        para_orm_list: list[KnowledgeParagraphORM] = []
        seen_para_hashes: set[str] = set()

        for rp in raw_paras:
            content_str = rp.content.strip()
            if not content_str:
                continue
            h = _h(content_str)
            if h in seen_para_hashes:
                continue
            seen_para_hashes.add(h)

            para = KnowledgeParagraphORM(
                document_id=doc_id,
                project_id=project_id_str,
                content=content_str,
                summary=rp.summary,
                symbol_names=rp.symbol_names,
                section_heading=rp.section_heading,
                heading_path=rp.heading_path,
                paragraph_index=rp.paragraph_index,
                token_count=rp.token_count,
                content_hash=h,
                source_path=source_path,
                source_start_line=rp.source_start_line,
                source_end_line=rp.source_end_line,
                branch_name=branch_name,
                branch_scope=branch_scope or "global",
                source_revision=source_revision,
                commit_sha=commit_sha,
                is_stale=False,
            )
            self._session.add(para)
            self._session.flush()
            para_orm_list.append(para)

            paragraph_fts_insert(
                self._session,
                paragraph_id=para.paragraph_id,
                content=content_str,
                summary=rp.summary,
                section_heading=rp.section_heading,
                symbol_names=rp.symbol_names,
            )

        # --- Propositions (extracted from full document content) ---
        raw_props = extract_propositions(
            redacted_content, source_type=source_type, source_path=source_path
        )
        seen_prop_hashes: set[str] = set()

        for rp_prop in raw_props:
            if rp_prop.content_hash in seen_prop_hashes:
                continue
            seen_prop_hashes.add(rp_prop.content_hash)

            prop = KnowledgePropositionORM(
                document_id=doc_id,
                project_id=project_id_str,
                proposition_text=rp_prop.proposition_text,
                normalized_text=rp_prop.normalized_text,
                proposition_type=rp_prop.proposition_type,
                confidence=rp_prop.confidence,
                content_hash=rp_prop.content_hash,
                source_path=source_path,
                source_start_line=rp_prop.source_start_line,
                source_end_line=rp_prop.source_end_line,
                branch_name=branch_name,
                branch_scope=branch_scope or "global",
                source_revision=source_revision,
                commit_sha=commit_sha,
                is_stale=False,
            )
            self._session.add(prop)
            self._session.flush()

            proposition_fts_insert(
                self._session,
                proposition_id=prop.proposition_id,
                proposition_text=prop.proposition_text,
                proposition_type=prop.proposition_type,
            )

        # --- Module-level summary ---
        if para_orm_list:
            # Wrap ORM list back to RawParagraph for summarizer
            from memory_engine.knowledge.paragraph_segmenter import RawParagraph as _RP
            raw_para_list_for_summary = [
                _RP(
                    content=p.content,
                    summary=p.summary,
                    symbol_names=p.symbol_names or [],
                    section_heading=p.section_heading,
                    heading_path=p.heading_path or [],
                    paragraph_index=p.paragraph_index,
                    source_path=p.source_path,
                    source_start_line=p.source_start_line,
                    source_end_line=p.source_end_line,
                )
                for p in para_orm_list
            ]
            mod_summary = summarize_module(raw_para_list_for_summary, source_path=source_path)
            if mod_summary:
                summ_hash = _h(mod_summary.content_hash_input)
                summ_orm = KnowledgeChunkSummaryORM(
                    document_id=doc_id,
                    project_id=project_id_str,
                    summary_text=mod_summary.summary_text,
                    purpose=mod_summary.purpose,
                    key_symbols=mod_summary.key_symbols,
                    responsibilities=mod_summary.responsibilities,
                    constraints_mentioned=mod_summary.constraints_mentioned,
                    important_interactions=mod_summary.important_interactions,
                    granularity_level=mod_summary.granularity_level,
                    content_hash=summ_hash,
                    source_path=source_path,
                    source_start_line=mod_summary.source_start_line,
                    source_end_line=mod_summary.source_end_line,
                    token_count=mod_summary.token_count,
                    branch_name=branch_name,
                    branch_scope=branch_scope or "global",
                    source_revision=source_revision,
                    commit_sha=commit_sha,
                    is_stale=False,
                )
                self._session.add(summ_orm)
                self._session.flush()

                summary_fts_insert(
                    self._session,
                    summary_id=summ_orm.summary_id,
                    summary_text=mod_summary.summary_text,
                    purpose=mod_summary.purpose,
                    key_symbols=mod_summary.key_symbols,
                    granularity_level=mod_summary.granularity_level,
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
