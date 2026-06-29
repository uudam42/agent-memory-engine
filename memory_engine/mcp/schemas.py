"""Pydantic models for MCP tool inputs and outputs."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared metadata
# ---------------------------------------------------------------------------


class RetrievalMeta(BaseModel):
    retrieval_mode: str = "lexical_structured_fallback"
    vector_backend: str = "ephemeral"
    bootstrap_status: str = "READY"
    warnings: list[str] = Field(default_factory=list)
    # Phase 9: git context fields
    current_branch: str | None = None
    head_commit: str | None = None
    branch_aware_ranking: bool = False
    git_available: bool = False
    is_repository: bool = False
    # Phase 13: semantic retrieval trace (additive, backward-compatible)
    semantic_backend: str = "none"
    embedding_provider: str = "none"
    embedding_model: str = "none"
    semantic_status: str = "disabled"   # disabled | unavailable | used | degraded


# ---------------------------------------------------------------------------
# Tool inputs
# ---------------------------------------------------------------------------


class RetrieveContextInput(BaseModel):
    task: str
    current_files: list[str] = Field(default_factory=list)
    current_symbols: list[str] = Field(default_factory=list)
    token_budget: int = 6000
    user_instruction_flags: dict[str, Any] = Field(default_factory=dict)
    # Phase 9: optional branch context (caller can override auto-detection)
    current_branch: str | None = None
    head_commit: str | None = None
    # Phase 10: multi-granularity routing hints
    task_intent: str = "unknown"
    preferred_layers: list[str] = Field(default_factory=list)
    proposition_types: list[str] | None = None


class InspectMemoryInput(BaseModel):
    memory_id: str
    inspection_depth: int = 1
    include_evidence: bool = False
    current_task: str | None = None


class InspectKnowledgeInput(BaseModel):
    chunk_id: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    include_content: bool = True


class ReflectAndWriteInput(BaseModel):
    task: str
    outcome: str
    verification_status: str = "unverified"
    changed_files: list[str] = Field(default_factory=list)
    task_summary: str | None = None
    test_summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    user_instruction_flags: dict[str, Any] = Field(default_factory=dict)
    # Phase 9: branch context for scoped memory writes
    current_branch: str | None = None
    head_commit: str | None = None
    # Phase 11: allow agent to pass intent explicitly instead of relying on inference
    task_intent: str | None = None


class SeedProjectInput(BaseModel):
    description: str = ""
    constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    conventions: list[str] = Field(default_factory=list)
    skip_auto_extract: bool = False


class SeedProjectOutput(BaseModel):
    nodes_created: int = 0
    module_nodes: int = 0
    constraint_nodes: int = 0
    decision_nodes: int = 0
    procedure_nodes: int = 0
    node_titles: list[str] = Field(default_factory=list)
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------
# Tool outputs
# ---------------------------------------------------------------------------


class RetrieveContextOutput(BaseModel):
    task: str
    constraints: list[Any] = Field(default_factory=list)
    architecture: list[Any] = Field(default_factory=list)
    modules: list[Any] = Field(default_factory=list)
    decisions: list[Any] = Field(default_factory=list)
    incidents: list[Any] = Field(default_factory=list)
    procedures: list[Any] = Field(default_factory=list)
    knowledge_chunks: list[Any] = Field(default_factory=list)
    # Phase 10: multi-granularity results (proposition / paragraph / summary)
    multigranular_chunks: list[Any] = Field(default_factory=list)
    retrieval_trace: list[Any] = Field(default_factory=list)
    total_token_estimate: int = 0
    memory_results_count: int = 0
    knowledge_results_count: int = 0
    multigranular_results_count: int = 0
    meta: RetrievalMeta = Field(default_factory=RetrievalMeta)


class ReflectWriteOutput(BaseModel):
    outcome: Literal["persisted", "skipped", "needs_review"]
    skip_reason: str | None = None
    candidates_staged: int = 0
    candidates_promoted: int = 0
    candidates_discarded: int = 0
    consolidation_notes: list[str] = Field(default_factory=list)
    meta: RetrievalMeta = Field(default_factory=RetrievalMeta)


class MemoryStatusOutput(BaseModel):
    project_name: str
    project_root: str
    bootstrap_status: str
    retrieval_mode: str
    vector_backend: str
    active_memories: int = 0
    stale_memories: int = 0
    knowledge_documents: int = 0
    knowledge_chunks: int = 0
    last_indexed_at: str = ""
    project_revision: str = ""
    memory_revision: int = 0
    knowledge_revision: int = 0
    index_revision: int = 0
    cache_enabled: bool = True
    warnings: list[str] = Field(default_factory=list)
    # Phase 9: Git and branch state
    current_branch: str | None = None
    head_commit: str | None = None
    base_branch: str | None = None
    git_available: bool = False
    is_repository: bool = False
    working_tree_dirty: bool = False
    staged_files_count: int = 0
    modified_files_count: int = 0
    last_git_sync_at: str = ""
    branch_aware_retrieval_enabled: bool = False
    synchronization_status: str = "idle"
    # Phase 13: semantic retrieval status (additive)
    semantic_enabled: bool = False
    semantic_backend: str = "none"
    embedding_provider: str = "none"
    embedding_model: str = "none"
    embedded_record_count: int = 0
    pending_embedding_count: int = 0
    semantic_health: str = "disabled"   # disabled | unavailable | healthy | degraded
