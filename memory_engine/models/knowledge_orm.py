"""SQLAlchemy 2.x ORM mapped classes for Knowledge Base.

Tables (Phase 6):
  knowledge_documents  — one row per ingested document
  knowledge_chunks     — one row per content chunk (separate from memory_nodes)
  knowledge_links      — typed links: memory↔chunk, chunk↔chunk
  knowledge_index_jobs — async/deferred indexing job tracking
  knowledge_chunks_fts — FTS5 virtual table (created via raw DDL in init_db)

Tables (Phase 10 — multi-granularity):
  knowledge_paragraphs    — coherent local context units (function, section, test cluster)
  knowledge_propositions  — atomic factual/behavioral statements
  knowledge_chunk_summaries — concise summaries of larger regions

Separation principle: these tables never overlap with memory_nodes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Import the shared Base (same metadata — all tables share one engine)
from memory_engine.models.orm import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class KnowledgeDocumentORM(Base):
    __tablename__ = "knowledge_documents"

    document_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # Phase 9: extended branch metadata
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, default="global")
    source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_from_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_to_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # Phase 10: document/module/architecture level summaries
    document_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    module_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    architecture_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    dependencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    related_documents: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    chunks: Mapped[list[KnowledgeChunkORM]] = relationship(
        "KnowledgeChunkORM",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    paragraphs: Mapped[list[KnowledgeParagraphORM]] = relationship(
        "KnowledgeParagraphORM",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    propositions: Mapped[list[KnowledgePropositionORM]] = relationship(
        "KnowledgePropositionORM",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    chunk_summaries: Mapped[list[KnowledgeChunkSummaryORM]] = relationship(
        "KnowledgeChunkSummaryORM",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class KnowledgeChunkORM(Base):
    __tablename__ = "knowledge_chunks"

    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heading_path: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    module_paths: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    related_symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_note")
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # Phase 9: branch-aware metadata
    branch_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, default="global")
    source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    document: Mapped[KnowledgeDocumentORM] = relationship(
        "KnowledgeDocumentORM", back_populates="chunks"
    )


class KnowledgeLinkORM(Base):
    __tablename__ = "knowledge_links"

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)

    # Source: either a memory_node or a knowledge_chunk
    source_memory_node_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("memory_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_chunk_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_chunks.chunk_id", ondelete="CASCADE"),
        nullable=True,
    )

    # Target: either a knowledge_chunk or a memory_node
    target_chunk_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_chunks.chunk_id", ondelete="CASCADE"),
        nullable=True,
    )
    target_memory_node_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("memory_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class KnowledgeIndexJobORM(Base):
    __tablename__ = "knowledge_index_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    document_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.document_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    chunks_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# ---------------------------------------------------------------------------
# Phase 10: Multi-Granularity Knowledge Models
# ---------------------------------------------------------------------------


class KnowledgeParagraphORM(Base):
    """Coherent local context unit — function, section, test cluster, doc paragraph."""

    __tablename__ = "knowledge_paragraphs"

    paragraph_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    section_heading: Mapped[str | None] = mapped_column(String(512), nullable=True)
    heading_path: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    paragraph_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Branch / revision provenance
    branch_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, default="global")
    source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    document: Mapped[KnowledgeDocumentORM] = relationship(
        "KnowledgeDocumentORM", back_populates="paragraphs"
    )
    propositions: Mapped[list[KnowledgePropositionORM]] = relationship(
        "KnowledgePropositionORM",
        back_populates="paragraph",
        cascade="all, delete-orphan",
    )


class KnowledgePropositionORM(Base):
    """Atomic factual or behavioral statement — highest retrieval precision."""

    __tablename__ = "knowledge_propositions"

    proposition_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    paragraph_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_paragraphs.paragraph_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    proposition_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    proposition_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="implementation_detail"
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Branch / revision provenance
    branch_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, default="global")
    source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    document: Mapped[KnowledgeDocumentORM] = relationship(
        "KnowledgeDocumentORM", back_populates="propositions"
    )
    paragraph: Mapped[KnowledgeParagraphORM | None] = relationship(
        "KnowledgeParagraphORM", back_populates="propositions"
    )


class KnowledgeChunkSummaryORM(Base):
    """Concise summary of a cohesive source region (module, subsystem, test suite)."""

    __tablename__ = "knowledge_chunk_summaries"

    summary_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    responsibilities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    constraints_mentioned: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    important_interactions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    granularity_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="chunk"
    )  # chunk | module | document
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Branch / revision provenance
    branch_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    branch_scope: Mapped[str | None] = mapped_column(String(32), nullable=True, default="global")
    source_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    document: Mapped[KnowledgeDocumentORM] = relationship(
        "KnowledgeDocumentORM", back_populates="chunk_summaries"
    )
