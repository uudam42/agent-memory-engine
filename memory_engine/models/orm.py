"""SQLAlchemy 2.x ORM mapped classes.

Stage 2 additions to memory_nodes:
  - status   VARCHAR(16)  default 'active'
  - confidence FLOAT      default 1.0
  - importance FLOAT      default 0.5
  - module_path VARCHAR(512) nullable

Phase 3 additions:
  - memory_nodes.status enlarged to VARCHAR(32) to fit 'needs_review'
  - memory_candidates table (staging area)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class ProjectORM(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    memory_nodes: Mapped[list[MemoryNodeORM]] = relationship(
        "MemoryNodeORM", back_populates="project", cascade="all, delete-orphan"
    )
    candidates: Mapped[list[MemoryCandidateORM]] = relationship(
        "MemoryCandidateORM", back_populates="project", cascade="all, delete-orphan"
    )


class MemoryNodeORM(Base):
    __tablename__ = "memory_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("memory_nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # Stage 2 / Phase 3 metadata (VARCHAR(32) to fit 'needs_review')
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    module_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    project: Mapped[ProjectORM] = relationship("ProjectORM", back_populates="memory_nodes")
    parent: Mapped[MemoryNodeORM | None] = relationship(
        "MemoryNodeORM", remote_side="MemoryNodeORM.id", back_populates="children"
    )
    children: Mapped[list[MemoryNodeORM]] = relationship(
        "MemoryNodeORM", back_populates="parent"
    )
    evidence: Mapped[list[EvidenceORM]] = relationship(
        "EvidenceORM", back_populates="memory_node", cascade="all, delete-orphan"
    )


class EvidenceORM(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    memory_node_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("memory_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    memory_node: Mapped[MemoryNodeORM] = relationship(
        "MemoryNodeORM", back_populates="evidence"
    )


class MemoryRelationORM(Base):
    __tablename__ = "memory_relations"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relation_type", name="uq_relation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MemoryCandidateORM(Base):
    """Staging area for proposed memory nodes before promotion.

    A candidate remains here until PromotionService decides the outcome.
    """

    __tablename__ = "memory_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    proposed_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    proposed_module_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    proposed_parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    evidence_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Outcome fields — set after promotion
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    promote_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    project: Mapped[ProjectORM] = relationship("ProjectORM", back_populates="candidates")
