"""Phase 6 Knowledge Base API endpoints.

POST  /v1/knowledge/ingest
POST  /v1/knowledge/search
GET   /v1/knowledge/documents/{document_id}
GET   /v1/knowledge/chunks/{chunk_id}
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from memory_engine.api.deps import db_session
from memory_engine.knowledge.ingestion import (
    KnowledgeIngestionService,
    get_shared_vector_index,
)
from memory_engine.knowledge.search import KnowledgeSearchService
from memory_engine.models.knowledge_domain import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestRequest,
    KnowledgeIngestResult,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
)
from memory_engine.models.knowledge_orm import KnowledgeChunkORM, KnowledgeDocumentORM

router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])


# ── POST /v1/knowledge/ingest ─────────────────────────────────────────────────

@router.post("/ingest", response_model=KnowledgeIngestResult, status_code=201)
def ingest_document(
    payload: KnowledgeIngestRequest,
    session: Session = Depends(db_session),
) -> KnowledgeIngestResult:
    """Ingest a raw document into the knowledge base.

    The service handles redaction, chunking, FTS5 indexing, and vector indexing.
    Returns immediately (synchronous indexing path).
    Unchanged documents (same source_path + content_hash) are skipped.
    """
    svc = KnowledgeIngestionService(
        session=session, vector_index=get_shared_vector_index()
    )
    try:
        return svc.ingest(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── POST /v1/knowledge/search ─────────────────────────────────────────────────

@router.post("/search", response_model=list[KnowledgeSearchResult])
def search_knowledge(
    payload: KnowledgeSearchRequest,
    session: Session = Depends(db_session),
) -> list[KnowledgeSearchResult]:
    """Hybrid lexical + vector search across project knowledge chunks.

    Supports optional source_type filters and current_files / current_symbols
    context signals for module-path and symbol overlap bonuses.
    Returns de-duplicated, token-budget-trimmed results.
    """
    svc = KnowledgeSearchService(
        session=session, vector_index=get_shared_vector_index()
    )
    return svc.search(payload)


# ── GET /v1/knowledge/documents/{document_id} ─────────────────────────────────

@router.get("/documents/{document_id}", response_model=KnowledgeDocument)
def get_document(
    document_id: str,
    session: Session = Depends(db_session),
) -> KnowledgeDocument:
    """Return metadata for a single KnowledgeDocument."""
    doc = session.get(KnowledgeDocumentORM, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id!r} not found")

    chunk_count = (
        session.query(KnowledgeChunkORM)
        .filter_by(document_id=document_id, index_status="indexed")
        .count()
    )
    from memory_engine.models.knowledge_domain import DocumentStatus, SourceType
    return KnowledgeDocument(
        document_id=uuid.UUID(doc.document_id),
        project_id=uuid.UUID(doc.project_id),
        source_type=SourceType(doc.source_type),
        title=doc.title,
        source_path=doc.source_path,
        content_hash=doc.content_hash,
        version_ref=doc.version_ref,
        branch_name=doc.branch_name,
        status=DocumentStatus(doc.status),
        tags=doc.tags or [],
        chunk_count=chunk_count,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


# ── GET /v1/knowledge/chunks/{chunk_id} ──────────────────────────────────────

@router.get("/chunks/{chunk_id}", response_model=KnowledgeChunk)
def get_chunk(
    chunk_id: str,
    session: Session = Depends(db_session),
) -> KnowledgeChunk:
    """Return a single KnowledgeChunk by ID (debug / inspection use)."""
    chunk = session.get(KnowledgeChunkORM, chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail=f"Chunk {chunk_id!r} not found")

    from memory_engine.models.knowledge_domain import ChunkIndexStatus, SourceType
    return KnowledgeChunk(
        chunk_id=uuid.UUID(chunk.chunk_id),
        document_id=uuid.UUID(chunk.document_id),
        project_id=uuid.UUID(chunk.project_id),
        content=chunk.content,
        content_hash=chunk.content_hash,
        chunk_index=chunk.chunk_index,
        token_count=chunk.token_count,
        heading_path=chunk.heading_path or [],
        module_paths=chunk.module_paths or [],
        related_symbols=chunk.related_symbols or [],
        tags=chunk.tags or [],
        language=chunk.language,
        source_type=SourceType(chunk.source_type),
        source_path=chunk.source_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        embedding_status=chunk.embedding_status,
        index_status=ChunkIndexStatus(chunk.index_status),
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )
