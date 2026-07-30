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
    Phase 9 additions: branch-aware relation types
    """

    depends_on = "depends_on"
    related_to = "related_to"
    supersedes = "supersedes"
    implements = "implements"
    # Phase 3
    supports = "supports"
    contradicts = "contradicts"
    derived_from = "derived_from"
    # Phase 9: branch-aware relations
    derived_from_branch = "derived_from_branch"
    inherited_from_mainline = "inherited_from_mainline"
    promoted_to_mainline = "promoted_to_mainline"
    invalidated_by_branch_change = "invalidated_by_branch_change"
    renamed_source = "renamed_source"


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
    # Phase 15: source-validity lifecycle (Issue 1)
    needs_revalidation = "needs_revalidation"  # source evidence changed — non-authoritative
    invalidated = "invalidated"                 # source evidence gone/broken — retained for audit


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


class ConstraintScope(StrEnum):
    """Issue 2 — the authority radius of a ``constraint`` (or similar
    always-relevant-by-default) memory node.

    Determines whether a constraint may bypass the topical relevance gate:
      global        — applies everywhere in every task, regardless of topic
                       (e.g. "never log secrets"). Bypasses relevance gating
                       only when additionally active, sufficiently confident,
                       and (once Issue 3 lands) sufficiently trusted.
      repository    — applies anywhere within this project/repository, but
                       is not asserted to be universally true. Default,
                       conservative fallback for legacy/unscoped constraints.
      branch        — applies only on a specific branch (node.branch_name).
      module        — applies only when the current task/module overlaps
                       node.module_path.
      path          — applies only when the current task touches
                       node.source_path directly.
      symbol        — applies only when the current task touches
                       node.source_symbol directly.
      task_intent   — applies only for a compatible task intent (matched via
                       an "intent:<value>" tag convention).
      needs_scope_review — scope could not be determined safely; the
                       constraint never bypasses the relevance gate until a
                       human/explicit process assigns a real scope.
    """

    global_ = "global"
    repository = "repository"
    branch = "branch"
    module = "module"
    path = "path"
    symbol = "symbol"
    task_intent = "task_intent"
    needs_scope_review = "needs_scope_review"


class SourceTrust(StrEnum):
    """Issue 3 — provenance-based trust level for a memory node's content.

    Assigned at creation time based on *where the content came from*
    (provenance) — never from the wording or imperative tone of the content
    itself (that would let injected text like "ignore previous instructions"
    gain authority merely by sounding authoritative). Ordered highest to
    lowest authority; see ``SOURCE_TRUST_ORDER`` below and
    ``memory_engine.services.source_trust`` for the eligibility logic that
    consumes it.

      human_confirmed_policy    — an explicit human action approved this as
                                   authoritative policy. Not reachable
                                   automatically by any pipeline in this
                                   codebase today (documented, not faked) —
                                   only ``source_trust.apply_trust_transition``
                                   (an explicit, auditable elevation) can set it.
      reviewed_committed_design — architecture/ADR/decision docs that are part
                                   of the repository's committed, reviewed
                                   source (mirrors bootstrap's seed-file
                                   patterns for docs/architecture/ADR paths).
      committed_source_or_test  — ordinary committed code/tests: evidence of
                                   behavior, not asserted policy.
      generated_report          — content generated by the system itself
                                   (e.g. reflection-derived candidates with no
                                   single committed-file source, bootstrap
                                   reports). Default for agent-asserted
                                   "discovered_constraints" text with no
                                   source_path — this is exactly the case a
                                   prompt-injection attempt would exploit, so
                                   it deliberately sits below the
                                   authoritative-context threshold.
      diff_or_log               — diffs, logs, commit messages: low trust,
                                   ephemeral, easily attacker-influenced.
      imported_or_external      — content ingested from outside the
                                   repository (no such MemoryNode-creation
                                   path exists yet; reserved for when one
                                   does).
      unknown                   — default when provenance cannot be
                                   determined, and the default for legacy
                                   nodes with no trust_level recorded at all.
                                   Never satisfies an authority threshold.
    """

    human_confirmed_policy = "human_confirmed_policy"
    reviewed_committed_design = "reviewed_committed_design"
    committed_source_or_test = "committed_source_or_test"
    generated_report = "generated_report"
    diff_or_log = "diff_or_log"
    imported_or_external = "imported_or_external"
    unknown = "unknown"


# Ordinal ranking (higher = more authoritative). Single source of truth,
# reused by memory_engine.services.source_trust for threshold comparisons so
# domain.py (no service-layer imports) and the trust service stay consistent.
SOURCE_TRUST_ORDER: dict[str, int] = {
    SourceTrust.human_confirmed_policy.value: 6,
    SourceTrust.reviewed_committed_design.value: 5,
    SourceTrust.committed_source_or_test.value: 4,
    SourceTrust.generated_report.value: 3,
    SourceTrust.diff_or_log.value: 2,
    SourceTrust.imported_or_external.value: 1,
    SourceTrust.unknown.value: 0,
}

# Minimum trust an authoritative-kind memory (constraint/architecture/
# decision) must carry before it may be treated as authoritative context
# (global-scope bypass, high-confidence decision, authoritative architecture).
# This is the Issue 3 replacement for Issue 2's `confidence >= 0.85` stand-in.
MIN_AUTHORITATIVE_TRUST = SourceTrust.reviewed_committed_design

# Memory kinds where "authority" (as opposed to mere retrievability as
# evidence) is a meaningful concept. No MemoryKind.security_rule exists yet;
# a highly-sensitive `constraint` is the closest equivalent (Issue 3 spec).
AUTHORITATIVE_KINDS = frozenset({"constraint", "architecture", "decision"})


class VerificationEvidenceLevel(StrEnum):
    """Issue 4 — how trustworthy is the *claim* that a task's outcome was
    verified (tests passed / build succeeded / etc.)?

    This is a different axis from ``VerificationStatus`` (what kind of check
    was claimed to have run: tests / build / manual / none) and from
    ``SourceTrust`` (where a memory's *content* came from). This enum
    answers: who or what actually observed the verification evidence, and
    how much should that observation be trusted?

      unverified         — no verification claim at all. Lowest.
      agent_claimed       — an agent asserted success with no structured,
                             independently-checkable evidence attached. This
                             is what legacy ``VerificationStatus.tests_passed``/
                             ``build_success``/``manual_check`` values map to
                             by default (see
                             ``memory_engine.services.verification_evidence``)
                             — an agent's self-report is not proof.
      engine_observed     — Memory Engine itself observed structured evidence
                             via a trusted, first-party execution path (e.g.
                             it ran the test command and captured the exit
                             code itself). No such execution path exists in
                             this codebase today (Issue 4 explicitly forbids
                             adding one — see module docstring on
                             ``verification_evidence.py``), so this level is
                             documented as currently unreachable in
                             production, exactly like Issue 3's
                             ``human_confirmed_policy`` before an explicit
                             elevation call is made.
      external_observed   — structured evidence supplied by an external
                             system (e.g. a CI run), not directly executed by
                             Memory Engine, but carrying an external
                             reference (CI run URL/ID) making it
                             independently checkable.
      human_confirmed     — a human explicitly confirmed the verification.
                             Only reachable via an explicit, auditable
                             elevation call (mirrors Issue 3's
                             ``apply_trust_transition``) — never
                             automatically inferred. Highest.

    Ordering (lowest to highest authority) is captured in
    ``VERIFICATION_EVIDENCE_ORDER`` below.
    """

    unverified = "unverified"
    agent_claimed = "agent_claimed"
    engine_observed = "engine_observed"
    external_observed = "external_observed"
    human_confirmed = "human_confirmed"


# Ordinal ranking (higher = more trustworthy evidence), mirroring
# SOURCE_TRUST_ORDER's single-source-of-truth convention.
VERIFICATION_EVIDENCE_ORDER: dict[str, int] = {
    VerificationEvidenceLevel.unverified.value: 0,
    VerificationEvidenceLevel.agent_claimed.value: 1,
    VerificationEvidenceLevel.external_observed.value: 2,
    VerificationEvidenceLevel.engine_observed.value: 3,
    VerificationEvidenceLevel.human_confirmed.value: 4,
}


class VerificationEvidence(BaseModel):
    """Issue 4 — compact, structured evidence supporting a verification
    claim. Never raw logs (rule: do not store large raw logs by default) —
    only small, comparable fields.

    All fields optional; a caller supplies whichever it actually has. Never
    invented/guessed by the system — absence of a field simply means that
    signal was not available.
    """

    target: str | None = None                 # e.g. "pytest tests/"
    exit_code: int | None = None
    observed_at: datetime | None = None
    output_digest: str | None = None           # short hash, NOT raw output
    source_commit: str | None = None           # reuse Phase 9 branch-awareness
    source_branch: str | None = None
    working_tree_dirty: bool | None = None     # reuse Phase 9 GitContext concept
    changed_file_digest: str | None = None
    observer: str | None = None                # "agent", "ci:github-actions", a username
    external_ref: str | None = None            # CI run URL/ID, optional


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
    # Phase 15 (Issue 1): optional source evidence for automatic stale detection.
    source_path: str | None = None
    source_hash: str | None = None
    # Phase 15 follow-up: optional symbol evidence (Task 8) — only set when the
    # caller identified exactly one symbol inside the single source_path file.
    source_symbol: str | None = None
    # Issue 2: explicit constraint scope. None on non-constraint kinds and on
    # constraints whose scope was not explicitly determined (effective scope
    # is then derived conservatively — see constraint_scope service).
    constraint_scope: str | None = None
    # Path reference for ConstraintScope.path eligibility only — deliberately
    # independent of source_path/source_hash (see MemoryNodeORM docstring).
    constraint_scope_ref: str | None = None
    # Issue 3: explicit provenance-based trust level. None means "let the
    # creating service assign a conservative default" (see
    # memory_engine.services.source_trust.assign_creation_trust) — direct
    # callers (tests, API) may also set this explicitly.
    trust_level: str | None = None
    # Issue 4: explicit verification-evidence level. None means "let the
    # creating service assign the conservative default" (see
    # memory_engine.services.verification_evidence.assign_creation_evidence_level)
    # — direct callers (tests, API) may also set this explicitly, but only
    # engine_observed/external_observed are honored without an explicit
    # elevation call; human_confirmed set here is not trusted (creation is
    # never a sanctioned path to human_confirmed).
    evidence_level: str | None = None
    verification_evidence: VerificationEvidence | None = None


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

    # Phase 9: branch-aware fields (nullable — backward compatible)
    branch_name: str | None = None
    branch_scope: str | None = None
    commit_sha: str | None = None
    source_revision: str | None = None
    branch_promotion_eligible: bool = False
    source_path: str | None = None     # file path from evidence/source context

    # Phase 15: source-validity lifecycle (Issue 1) — nullable, backward compatible.
    source_hash: str | None = None            # sha256 of source file content at write time
    validity_reason: str | None = None        # why current status was set by validity checks
    validity_checked_at: datetime | None = None
    previous_status: MemoryStatus | None = None  # audit trail for last automatic transition
    source_symbol: str | None = None          # optional symbol-level evidence (Task 8)

    # Issue 2: explicit constraint scope (nullable — see ConstraintScope).
    constraint_scope: str | None = None
    constraint_scope_ref: str | None = None

    # Issue 3: provenance-based trust lifecycle — nullable, backward
    # compatible. A node with trust_level=None is read as SourceTrust.unknown
    # (see memory_engine.services.source_trust.effective_trust) — legacy rows
    # never silently gain authority.
    trust_level: str | None = None
    trust_reason: str | None = None
    trust_set_at: datetime | None = None
    previous_trust: str | None = None
    trust_elevated_by: str | None = None
    trust_elevated_reason: str | None = None
    trust_elevated_at: datetime | None = None

    # Issue 4: verification-evidence lifecycle — nullable, backward
    # compatible. A node with evidence_level=None is read as
    # VerificationEvidenceLevel.unverified (see
    # memory_engine.services.verification_evidence.effective_evidence_level)
    # — legacy rows never silently gain verification authority.
    evidence_level: str | None = None
    verification_evidence: VerificationEvidence | None = None
    evidence_reason: str | None = None
    evidence_set_at: datetime | None = None
    previous_evidence_level: str | None = None
    evidence_elevated_by: str | None = None
    evidence_elevated_reason: str | None = None
    evidence_elevated_at: datetime | None = None


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
    current_branch: str | None = None


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


def _is_low_trust_authoritative(node: MemoryNode) -> bool:
    """Issue 3: whether ``node`` needs the UNTRUSTED_REPOSITORY_CONTENT label
    when composed into context text.

    Scoped to authoritative-kind nodes only (constraint/architecture/
    decision) so ordinary module/procedure/debug/outcome content — including
    legitimate README/documentation-derived module summaries — is never
    labeled or otherwise degraded (Issue 3 requirement: don't penalize
    ordinary documentation retrieval or imperative build instructions).
    """
    if node.kind.value not in AUTHORITATIVE_KINDS:
        return False
    level = node.trust_level or SourceTrust.unknown.value
    rank = SOURCE_TRUST_ORDER.get(level, SOURCE_TRUST_ORDER[SourceTrust.unknown.value])
    return rank < SOURCE_TRUST_ORDER[MIN_AUTHORITATIVE_TRUST.value]


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
                if _is_low_trust_authoritative(n):
                    # Issue 3: compact, non-token-heavy marker — content is
                    # still shown (evidence-only retrieval is not degraded)
                    # but must not be read as an authoritative instruction.
                    lines.append("    UNTRUSTED_REPOSITORY_CONTENT")
                    lines.append("    authority: evidence-only")
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
    # Phase 15 follow-up (Task 2): optional project-relative source evidence,
    # derived deterministically by ReflectionSkill when a single touched file
    # (and optionally a single touched symbol) unambiguously identifies the
    # candidate's origin. Left None when evidence is absent or spans multiple
    # files with no single declared primary source — never guessed.
    source_path: str | None = None
    source_symbol: str | None = None
    # Issue 2: proposed scope for constraint-kind candidates. Derived
    # conservatively by ReflectionSkill — never inferred as 'global' purely
    # from proposed_kind == constraint.
    proposed_constraint_scope: str | None = None
    proposed_constraint_scope_ref: str | None = None
    # Issue 4: verification-evidence level/data proposed for the resulting
    # node, derived conservatively by ReflectionSkill (never invented).
    proposed_evidence_level: str | None = None
    proposed_verification_evidence: VerificationEvidence | None = None


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

    # Phase 9: branch context for scoped memory writes
    branch_name: str | None = None
    head_commit: str | None = None
    branch_scope: str | None = None   # current_branch | mainline | global
    module_path: str | None = None        # primary affected module (dotted path)
    task_metadata: dict[str, Any] = Field(default_factory=dict)

    # Issue 4: optional structured verification evidence and an explicit
    # asserted level. Legacy callers omit both and continue to work exactly
    # as before — verification_status alone still determines candidate
    # confidence (unchanged), while evidence_level is derived conservatively
    # (see memory_engine.services.verification_evidence). asserted_level is
    # only honored for engine_observed/external_observed when accompanied by
    # matching structured evidence; human_confirmed is never honored here
    # (only reachable via an explicit elevation call after creation).
    verification_evidence: VerificationEvidence | None = None
    asserted_evidence_level: VerificationEvidenceLevel | None = None


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
