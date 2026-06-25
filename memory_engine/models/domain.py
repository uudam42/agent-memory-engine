"""Pydantic v2 domain models — the canonical data contracts used across all layers.

Stage 2 additions:
- MemoryStatus, TaskIntent, TaskComplexity, RiskLevel, MemoryType enums
- Extended MemoryNode with status / confidence / importance / module_path
- RoutingPlan, RouteRequest
- RecallRequest, RecallResult, TraceEntry, ScoredMemory
- InspectRequest, InspectResult, ConfidenceAssessment
- EnrichedContextPack

Phase 3 additions:
- MemoryStatus.needs_review
- RelationType: supports, contradicts, derived_from
- CandidateStatus, PromoteAction, ConflictKind enums
- CandidateCreate, PersistedCandidate
- PlacementDecision, DuplicateMatch, ConflictReport, PromoteResult

Phase 5 additions:
- VerificationStatus, TaskOutcome, ReflectionSkipReason enums
- ReflectionInput, ReflectionAnalysis, PostTaskResult
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Core enumerations (Stage 1)
# ---------------------------------------------------------------------------


class MemoryKind(StrEnum):
    """Semantic category of a memory node."""

    architecture = "architecture"
    module = "module"
    debug = "debug"           # incident / debugging record
    decision = "decision"
    procedure = "procedure"
    constraint = "constraint"
    outcome = "outcome"


class RelationType(StrEnum):
    """Directed relationship between two memory nodes.

    Phase 3 additions: supports, contradicts, derived_from
    """

    depends_on = "depends_on"
    related_to = "related_to"
    supersedes = "supersedes"
    implements = "implements"
    # Phase 3
    supports = "supports"
    contradicts = "contradicts"
    derived_from = "derived_from"


# ---------------------------------------------------------------------------
# Stage 2 enumerations
# ---------------------------------------------------------------------------


class MemoryStatus(StrEnum):
    """Lifecycle status of a memory node.

    Phase 3 addition: needs_review
    """

    active = "active"
    stale = "stale"
    superseded = "superseded"
    archived = "archived"
    needs_review = "needs_review"   # Phase 3: unresolved conflict — human required


class TaskIntent(StrEnum):
    bug_fix = "bug_fix"
    feature_implementation = "feature_implementation"
    refactor = "refactor"
    architecture_review = "architecture_review"
    code_explanation = "code_explanation"
    test_failure = "test_failure"
    repository_onboarding = "repository_onboarding"
    workflow_question = "workflow_question"
    documentation = "documentation"
    trivial_edit = "trivial_edit"
    unknown = "unknown"


class TaskComplexity(StrEnum):
    trivial = "trivial"
    low = "low"
    medium = "medium"
    high = "high"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class MemoryType(StrEnum):
    semantic_memory = "semantic_memory"
    procedural_memory = "procedural_memory"
    decision_memory = "decision_memory"
    incident_memory = "incident_memory"
    constraint_memory = "constraint_memory"
    preference_memory = "preference_memory"


# ---------------------------------------------------------------------------
# Phase 3 enumerations
# ---------------------------------------------------------------------------


class CandidateStatus(StrEnum):
    """Lifecycle of a MemoryCandidate in the staging area."""

    pending = "pending"
    promoted = "promoted"
    discarded = "discarded"
    needs_review = "needs_review"


class PromoteAction(StrEnum):
    """What the PromotionService decided to do with a candidate."""

    create = "create"           # inserted as a new node
    merge = "merge"             # merged into an existing node
    update = "update"           # existing node updated with new info
    discard = "discard"         # no new information; existing node sufficient
    supersede = "supersede"     # candidate supersedes and retires existing node
    needs_review = "needs_review"   # unresolved conflict — human must decide


class ConflictKind(StrEnum):
    confidence_too_low = "confidence_too_low"       # candidate can't overwrite high-conf
    content_contradiction = "content_contradiction" # summaries appear contradictory
    unresolved = "unresolved"                       # needs human review


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: str | None = None


class ProjectCreate(ProjectBase):
    pass


class Project(ProjectBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class EvidenceBase(BaseModel):
    content: Annotated[str, Field(min_length=1)]
    source: str | None = None


class EvidenceCreate(EvidenceBase):
    memory_node_id: UUID


class Evidence(EvidenceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    memory_node_id: UUID
    created_at: datetime


# ---------------------------------------------------------------------------
# MemoryNode
# ---------------------------------------------------------------------------


class MemoryNodeBase(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(min_length=1)]
    kind: MemoryKind
    tags: list[str] = Field(default_factory=list)


class MemoryNodeCreate(MemoryNodeBase):
    project_id: UUID
    parent_id: UUID | None = None
    status: MemoryStatus = MemoryStatus.active
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    importance: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    module_path: str | None = None


class MemoryNode(MemoryNodeBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    parent_id: UUID | None = None
    depth: int = 0
    created_at: datetime
    updated_at: datetime
    evidence: list[Evidence] = Field(default_factory=list)
    status: MemoryStatus = MemoryStatus.active
    confidence: float = 1.0
    importance: float = 0.5
    module_path: str | None = None


# ---------------------------------------------------------------------------
# MemoryRelation
# ---------------------------------------------------------------------------


class MemoryRelationBase(BaseModel):
    source_id: UUID
    target_id: UUID
    relation_type: RelationType


class MemoryRelationCreate(MemoryRelationBase):
    pass


class MemoryRelation(MemoryRelationBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime


# ---------------------------------------------------------------------------
# Stage 1 ContextPack (backward compat)
# ---------------------------------------------------------------------------


class ContextPack(BaseModel):
    project: Project
    nodes: list[MemoryNode]
    relations: list[MemoryRelation] = Field(default_factory=list)
    total: int = 0

    def as_text(self) -> str:
        lines: list[str] = [f"# ContextPack — {self.project.name}", ""]
        for node in self.nodes:
            indent = "  " * node.depth
            lines.append(f"{indent}[{node.kind}] {node.title}")
            lines.append(f"{indent}  {node.summary}")
            for ev in node.evidence:
                lines.append(f"{indent}    evidence: {ev.content}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage 2 — Skill Router models
# ---------------------------------------------------------------------------


class RouteRequest(BaseModel):
    project_id: UUID
    current_task: Annotated[str, Field(min_length=1)]
    current_files: list[str] = Field(default_factory=list)
    current_symbols: list[str] = Field(default_factory=list)
    task_metadata: dict[str, Any] = Field(default_factory=dict)
    user_instruction_flags: list[str] = Field(default_factory=list)
    risk_hint: RiskLevel | None = None


class RoutingPlan(BaseModel):
    should_recall_memory: bool
    should_allow_deep_inspection: bool
    task_intent: TaskIntent
    task_complexity: TaskComplexity
    risk_level: RiskLevel
    required_memory_types: list[MemoryType]
    recommended_token_budget: int
    reasoning: list[str]
    persistence_allowed: bool


# ---------------------------------------------------------------------------
# Stage 2 — Recall models
# ---------------------------------------------------------------------------


class RecallRequest(BaseModel):
    project_id: UUID
    current_task: Annotated[str, Field(min_length=1)]
    current_files: list[str] = Field(default_factory=list)
    current_symbols: list[str] = Field(default_factory=list)
    token_budget: int | None = None
    routing_plan: RoutingPlan | None = None


class TraceEntry(BaseModel):
    memory_id: str
    title: str
    action: Literal["selected", "excluded", "expanded"]
    reason: str
    score: float
    # Phase 4 enrichments — all optional so existing callers don't break
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    status: str = "unknown"
    tree_path: list[str] = Field(default_factory=list)


class ScoredMemory(BaseModel):
    node: MemoryNode
    score: float
    score_breakdown: dict[str, float]


class EnrichedContextPack(BaseModel):
    project: Project
    constraints: list[MemoryNode] = Field(default_factory=list)
    architecture: list[MemoryNode] = Field(default_factory=list)
    modules: list[MemoryNode] = Field(default_factory=list)
    decisions: list[MemoryNode] = Field(default_factory=list)
    incidents: list[MemoryNode] = Field(default_factory=list)
    procedures: list[MemoryNode] = Field(default_factory=list)
    evidence_refs: list[Evidence] = Field(default_factory=list)
    total_nodes: int = 0
    token_estimate: int = 0

    def as_text(self) -> str:
        lines: list[str] = [f"# Memory Context — {self.project.name}", ""]

        def _section(title: str, nodes: list[MemoryNode]) -> None:
            if not nodes:
                return
            lines.append(f"## {title}")
            for n in nodes:
                lines.append(f"  [{n.kind}] {n.title}  (confidence={n.confidence:.2f})")
                lines.append(f"    {n.summary}")
            lines.append("")

        _section("Constraints", self.constraints)
        _section("Architecture", self.architecture)
        _section("Modules", self.modules)
        _section("Decisions", self.decisions)
        _section("Incidents", self.incidents)
        _section("Procedures", self.procedures)

        if self.evidence_refs:
            lines.append("## Evidence References")
            for ev in self.evidence_refs:
                src = f" [{ev.source}]" if ev.source else ""
                lines.append(f"  - {ev.content[:120]}{src}")
            lines.append("")

        lines.append(f"_token estimate: {self.token_estimate}_")
        return "\n".join(lines)


class RecallResult(BaseModel):
    context_pack: EnrichedContextPack
    routing_plan: RoutingPlan
    retrieval_trace: list[TraceEntry]
    token_estimate: int
    recall_skipped: bool = False
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Stage 2 — Inspect models
# ---------------------------------------------------------------------------


class InspectRequest(BaseModel):
    project_id: UUID
    memory_id: str
    inspection_depth: Annotated[int, Field(ge=1, le=5)] = 1
    include_evidence: bool = True
    current_task: str | None = None


class ConfidenceAssessment(BaseModel):
    confidence: float
    status: MemoryStatus
    freshness: float


class InspectResult(BaseModel):
    memory: MemoryNode
    children: list[MemoryNode]
    related_memories: list[MemoryNode]
    evidence_refs: list[Evidence]
    conflicts: list[MemoryNode]
    inspection_trace: list[str]
    confidence_assessment: ConfidenceAssessment


# ---------------------------------------------------------------------------
# Phase 3 — MemoryCandidate (persisted staging area)
# ---------------------------------------------------------------------------


class CandidateCreate(BaseModel):
    """Input for creating a candidate in the staging area."""

    project_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=256)]
    summary: Annotated[str, Field(min_length=1)]
    proposed_kind: MemoryKind
    proposed_tags: list[str] = Field(default_factory=list)
    proposed_module_path: str | None = None
    proposed_parent_id: UUID | None = None   # hint — placement may override
    source_ref: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8
    importance: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    evidence_content: str | None = None     # optional inline evidence to attach
    evidence_source: str | None = None


class PersistedCandidate(CandidateCreate):
    """A candidate stored in the staging table, ready for promotion."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4)
    status: CandidateStatus = CandidateStatus.pending
    promote_action: PromoteAction | None = None
    target_node_id: UUID | None = None      # set after promotion
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Phase 3 — Promotion pipeline models
# ---------------------------------------------------------------------------


class PlacementDecision(BaseModel):
    """Where in the tree the candidate should live."""

    intended_depth: int
    parent_id: UUID | None = None
    parent_title: str | None = None
    placement_reason: str


class DuplicateMatch(BaseModel):
    """A potential duplicate found during deduplication."""

    existing_node: MemoryNode
    similarity_score: float          # composite [0, 1]
    title_similarity: float
    module_overlap: float
    is_same_kind: bool


class ConflictReport(BaseModel):
    """A detected conflict that may block direct promotion."""

    kind: ConflictKind
    existing_node: MemoryNode
    candidate_confidence: float
    existing_confidence: float
    reason: str


class PromoteResult(BaseModel):
    """Full output of the PromotionService for one candidate."""

    candidate_id: UUID
    action: PromoteAction
    target_node: MemoryNode | None = None
    placement: PlacementDecision
    duplicate_match: DuplicateMatch | None = None
    conflict_report: ConflictReport | None = None
    relations_created: list[MemoryRelation] = Field(default_factory=list)
    consolidation_notes: list[str] = Field(default_factory=list)
    needs_human_review: bool = False
    review_reason: str | None = None


# ---------------------------------------------------------------------------
# Phase 3 — Lifecycle request models
# ---------------------------------------------------------------------------


class MarkStaleRequest(BaseModel):
    """Request to mark a memory node as stale."""

    reason: str


class MarkStaleResult(BaseModel):
    node_id: UUID
    previous_status: MemoryStatus
    new_status: MemoryStatus
    reason: str


# MemoryCandidate kept for backward compat with Stage 1 (non-persisted form)
class MemoryCandidate(BaseModel):
    """Unvalidated snippet proposed for storage. Not persisted directly.
    Use CandidateCreate for the persisted staging workflow."""

    raw_text: str
    proposed_kind: MemoryKind | None = None
    proposed_tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 5 — Post-task reflection and automatic memory writing
# ---------------------------------------------------------------------------


class VerificationStatus(StrEnum):
    """How was the outcome of the task verified?"""
    tests_passed = "tests_passed"        # automated test suite passed
    build_success = "build_success"      # build / compile succeeded
    manual_check = "manual_check"        # human or agent manually verified
    tests_failed = "tests_failed"        # automated tests failed
    unverified = "unverified"            # no verification performed


class TaskOutcome(StrEnum):
    """High-level outcome of the completed task."""
    completed = "completed"                     # task fully done and verified
    partially_completed = "partially_completed" # meaningful progress, not complete
    reverted = "reverted"                       # changes rolled back
    failed = "failed"                           # task could not be completed


class ReflectionSkipReason(StrEnum):
    """Why the reflection decided NOT to create memory candidates."""
    task_failed = "task_failed"
    task_reverted = "task_reverted"
    trivial_change = "trivial_change"
    unverified_low_confidence = "unverified_low_confidence"
    low_value = "low_value"
    no_new_knowledge = "no_new_knowledge"


class ReflectionInput(BaseModel):
    """What the agent reports after completing a task.

    The agent fills this in without any user interaction.  It is the agent's
    own post-task summary — not a user-supplied command.
    """

    project_id: UUID

    # What was asked and what happened
    task_description: Annotated[str, Field(min_length=1)]
    task_outcome: TaskOutcome
    outcome_summary: Annotated[str, Field(min_length=1)]   # what actually changed

    # Context signals
    touched_files: list[str] = Field(default_factory=list)
    touched_symbols: list[str] = Field(default_factory=list)

    # Verification
    verification_status: VerificationStatus = VerificationStatus.unverified

    # Explicit knowledge the agent wants to surface
    discovered_constraints: list[str] = Field(default_factory=list)
    discovered_procedures: list[str] = Field(default_factory=list)

    # Agent self-assessment (0.0 – 1.0)
    agent_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8

    # Optional — helps the reflection skill classify more accurately
    task_intent: TaskIntent | None = None
    module_path: str | None = None        # primary affected module (dotted path)
    task_metadata: dict[str, Any] = Field(default_factory=dict)


class ReflectionAnalysis(BaseModel):
    """Internal output of ReflectionSkill.analyze() — not persisted."""

    worth_retaining: bool
    skip_reason: ReflectionSkipReason | None = None
    retention_reasoning: list[str]

    # Candidates generated (empty when skip_reason is set)
    suggested_candidates: list[CandidateCreate] = Field(default_factory=list)

    # Estimated quality of the generated candidates
    estimated_importance: float = 0.5
    estimated_confidence: float = 0.8


class PostTaskResult(BaseModel):
    """Full output of the post-task reflection pipeline.

    Returned by POST /v1/skills/reflect-and-write.
    The agent receives this and continues — no further user action required.
    """

    project_id: UUID
    reflection: ReflectionAnalysis

    # Results from the promotion pipeline
    promotion_results: list[PromoteResult] = Field(default_factory=list)

    # Summary counts
    candidates_staged: int = 0
    candidates_promoted: int = 0    # action in (create, update, merge, supersede)
    candidates_discarded: int = 0
    candidates_needs_review: int = 0

    # Notes from ConsolidationService
    consolidation_notes: list[str] = Field(default_factory=list)

    # Top-level flags
    reflection_skipped: bool = False
    skip_reason: ReflectionSkipReason | None = None
    source_ref: str | None = None
