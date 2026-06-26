"""Pydantic v2 domain models for Phase 6 Knowledge Base.

Separation principle:
  KnowledgeChunk  — source-grounded searchable content (raw).
  MemoryNode      — compressed, curated engineering understanding.

They are linked via KnowledgeLink but never merged.
"""

from __future__ import annotations

from datetime import datetime
from enum import auto
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum as _Enum

    class StrEnum(str, _Enum):  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# Source types
# ---------------------------------------------------------------------------


class SourceType(StrEnum):
    markdown = "markdown"
    readme = "readme"
    architecture_doc = "architecture_doc"
    adr = "adr"
    api_spec = "api_spec"
    code_file = "code_file"
    code_comment = "code_comment"
    test_report = "test_report"
    runtime_log = "runtime_log"
    git_diff = "git_diff"
    task_artifact = "task_artifact"
    manual_note = "manual_note"


class DocumentStatus(StrEnum):
    pending = "pending"
    indexed = "indexed"
    outdated = "outdated"
    error = "error"


class ChunkIndexStatus(StrEnum):
    pending = "pending"
    indexed = "indexed"
    stale = "stale"


class KnowledgeLinkRelation(StrEnum):
    supports = "supports"
    explains = "explains"
    implements = "implements"
    documents = "documents"
    contradicts = "contradicts"
    derived_from = "derived_from"
    validated_by = "validated_by"
    related_to = "related_to"


class IndexJobStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


# ---------------------------------------------------------------------------
# KnowledgeDocument
# ---------------------------------------------------------------------------


class KnowledgeDocumentCreate(BaseModel):
    project_id: UUID
    source_type: SourceType
    title: str
    source_path: str | None = None
    content: str                      # raw content — chunked on ingest
    version_ref: str | None = None    # git SHA / semver / timestamp label
    branch_name: str | None = None
    tags: list[str] = Field(default_factory=list)


class KnowledgeDocument(BaseModel):
    model_config = {"from_attributes": True}

    document_id: UUID
    project_id: UUID
    source_type: SourceType
    title: str
    source_path: str | None
    content_hash: str
    version_ref: str | None
    branch_name: str | None
    status: DocumentStatus
    tags: list[str]
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# KnowledgeChunk
# ---------------------------------------------------------------------------


class KnowledgeChunk(BaseModel):
    model_config = {"from_attributes": True}

    chunk_id: UUID
    document_id: UUID
    project_id: UUID
    content: str
    content_hash: str
    chunk_index: int
    token_count: int
    heading_path: list[str]           # ["# Top", "## Sub"] for Markdown
    module_paths: list[str]           # dotted module paths
    related_symbols: list[str]        # function / class / variable names
    tags: list[str]
    language: str | None              # python, typescript, etc.
    source_type: SourceType
    source_path: str | None
    start_line: int | None
    end_line: int | None
    embedding_status: str             # pending | done
    index_status: ChunkIndexStatus
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# KnowledgeLink
# ---------------------------------------------------------------------------


class KnowledgeLinkCreate(BaseModel):
    relation: KnowledgeLinkRelation
    # Exactly one source and one target must be set in each pair
    source_memory_node_id: UUID | None = None
    source_chunk_id: UUID | None = None
    target_chunk_id: UUID | None = None
    target_memory_node_id: UUID | None = None
    notes: str | None = None


class KnowledgeLink(BaseModel):
    model_config = {"from_attributes": True}

    link_id: UUID
    relation: KnowledgeLinkRelation
    source_memory_node_id: UUID | None
    source_chunk_id: UUID | None
    target_chunk_id: UUID | None
    target_memory_node_id: UUID | None
    notes: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# KnowledgeIndexJob
# ---------------------------------------------------------------------------


class KnowledgeIndexJob(BaseModel):
    model_config = {"from_attributes": True}

    job_id: UUID
    project_id: UUID
    document_id: UUID | None
    status: IndexJobStatus
    chunks_total: int
    chunks_done: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# KnowledgeSearchResult
# ---------------------------------------------------------------------------


class KnowledgeSearchResult(BaseModel):
    """A single knowledge result returned by KnowledgeSearchService."""

    chunk_id: UUID
    document_id: UUID
    title: str                     # document title + heading path
    source_path: str | None
    source_type: SourceType
    heading_path: list[str]
    language: str | None
    start_line: int | None
    end_line: int | None
    content_preview: str           # first ~400 chars
    module_paths: list[str]
    related_symbols: list[str]
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    status: ChunkIndexStatus
    version_ref: str | None = None
    selected: bool = True
    exclusion_reason: str | None = None


# ---------------------------------------------------------------------------
# Unified context (extends EnrichedContextPack)
# ---------------------------------------------------------------------------


class KnowledgeTraceEntry(BaseModel):
    """Trace entry for a knowledge result (mirrors TraceEntry for memory)."""

    result_type: Literal["memory", "knowledge", "evidence"] = "knowledge"
    result_id: str
    title: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    action: Literal["selected", "excluded"] = "selected"
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    source_type: str | None = None
    freshness: float = 0.5
    status: str = "indexed"
    heading_path: list[str] = Field(default_factory=list)
    reason: str = ""


class KnowledgeContextSection(BaseModel):
    """A rendered section of knowledge chunks within a UnifiedContextPack."""

    chunk_id: str
    title: str
    source_type: str
    source_path: str | None
    heading_path: list[str]
    start_line: int | None
    end_line: int | None
    content: str
    score: float
    token_count: int


class UnifiedContextPack(BaseModel):
    """A token-budgeted context pack combining memory + knowledge.

    Memory sections come first (constraints → architecture → modules →
    decisions/incidents → procedures → evidence).
    Knowledge sections follow, de-duplicated and token-trimmed.
    """

    project_id: UUID
    task: str

    # Memory sections (from EnrichedContextPack)
    constraints: list[Any] = Field(default_factory=list)
    architecture: list[Any] = Field(default_factory=list)
    modules: list[Any] = Field(default_factory=list)
    decisions: list[Any] = Field(default_factory=list)
    incidents: list[Any] = Field(default_factory=list)
    procedures: list[Any] = Field(default_factory=list)
    evidence_refs: list[Any] = Field(default_factory=list)

    # Knowledge sections
    knowledge_chunks: list[KnowledgeContextSection] = Field(default_factory=list)

    # Unified trace (memory + knowledge)
    retrieval_trace: list[KnowledgeTraceEntry] = Field(default_factory=list)

    # Budget info
    total_token_estimate: int = 0
    token_budget: int = 6000
    memory_tokens: int = 0
    knowledge_tokens: int = 0

    # Metadata
    memory_results_count: int = 0
    knowledge_results_count: int = 0
    cache_hit: bool = False


# ---------------------------------------------------------------------------
# Search request (input to UnifiedContextRetrievalService)
# ---------------------------------------------------------------------------


class UnifiedRetrievalRequest(BaseModel):
    project_id: UUID
    task: str
    current_files: list[str] = Field(default_factory=list)
    current_symbols: list[str] = Field(default_factory=list)
    token_budget: int = 6000
    include_knowledge: bool = True
    source_type_filter: list[SourceType] | None = None
    user_instruction_flags: dict[str, Any] = Field(default_factory=dict)
    # Phase 9: branch-aware retrieval
    current_branch: str | None = None
    head_commit: str | None = None
    modified_files: list[str] = Field(default_factory=list)


class KnowledgeIngestRequest(BaseModel):
    project_id: UUID
    source_type: SourceType
    title: str
    content: str
    source_path: str | None = None
    version_ref: str | None = None
    branch_name: str | None = None
    tags: list[str] = Field(default_factory=list)


class KnowledgeIngestResult(BaseModel):
    document_id: UUID
    chunks_created: int
    was_duplicate: bool
    was_updated: bool
    redaction_count: int = 0
    index_job_id: UUID | None = None


class KnowledgeSearchRequest(BaseModel):
    project_id: UUID
    query: str
    current_files: list[str] = Field(default_factory=list)
    current_symbols: list[str] = Field(default_factory=list)
    source_types: list[SourceType] | None = None
    token_budget: int = 3000
    max_results: int = 10


# ---------------------------------------------------------------------------
# Phase 10: Multi-Granularity Domain Models
# ---------------------------------------------------------------------------


class PropositionType(StrEnum):
    behavior = "behavior"
    constraint = "constraint"
    architecture = "architecture"
    security_rule = "security_rule"
    implementation_detail = "implementation_detail"
    decision = "decision"
    procedure = "procedure"
    risk = "risk"
    test_evidence = "test_evidence"


class GranularityLevel(StrEnum):
    proposition = "proposition"
    paragraph = "paragraph"
    chunk = "chunk"
    module = "module"
    document = "document"


class KnowledgeParagraph(BaseModel):
    model_config = {"from_attributes": True}

    paragraph_id: UUID
    document_id: UUID
    project_id: UUID
    content: str
    summary: str | None
    symbol_names: list[str]
    section_heading: str | None
    heading_path: list[str]
    paragraph_index: int
    token_count: int
    content_hash: str
    source_path: str | None
    source_start_line: int | None
    source_end_line: int | None
    branch_name: str | None
    branch_scope: str | None
    source_revision: str | None
    commit_sha: str | None
    is_stale: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeProposition(BaseModel):
    model_config = {"from_attributes": True}

    proposition_id: UUID
    document_id: UUID
    paragraph_id: UUID | None
    project_id: UUID
    proposition_text: str
    normalized_text: str
    proposition_type: PropositionType
    confidence: float
    content_hash: str
    source_path: str | None
    source_start_line: int | None
    source_end_line: int | None
    branch_name: str | None
    branch_scope: str | None
    source_revision: str | None
    commit_sha: str | None
    is_stale: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeChunkSummary(BaseModel):
    model_config = {"from_attributes": True}

    summary_id: UUID
    document_id: UUID
    project_id: UUID
    summary_text: str
    purpose: str | None
    key_symbols: list[str]
    responsibilities: list[str]
    constraints_mentioned: list[str]
    important_interactions: list[str]
    granularity_level: GranularityLevel
    content_hash: str
    source_path: str | None
    source_start_line: int | None
    source_end_line: int | None
    token_count: int
    branch_name: str | None
    branch_scope: str | None
    source_revision: str | None
    commit_sha: str | None
    is_stale: bool
    created_at: datetime
    updated_at: datetime


class MultiGranularSearchRequest(BaseModel):
    """Request for multi-granularity knowledge retrieval (Phase 10)."""

    project_id: UUID
    query: str
    current_files: list[str] = Field(default_factory=list)
    current_symbols: list[str] = Field(default_factory=list)
    task_intent: str = "unknown"     # TaskIntent value
    preferred_layers: list[str] = Field(default_factory=list)   # override router
    proposition_types: list[str] | None = None                  # override router
    max_results: int = 15
    token_budget: int = 4000
    include_stale: bool = False


class MultiGranularitySearchResult(BaseModel):
    """A retrieved unit from any granularity layer."""

    result_id: str
    result_type: str   # proposition | paragraph | chunk | chunk_summary | module_summary
    granularity: GranularityLevel
    content: str
    score: float
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    source_path: str | None = None
    source_start_line: int | None = None
    source_end_line: int | None = None
    branch_name: str | None = None
    branch_scope: str | None = None
    commit_sha: str | None = None
    is_stale: bool = False
    parent_id: str | None = None
    selection_reason: str = ""
    expanded_from: str | None = None
