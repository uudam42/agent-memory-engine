"""Regression tests for Issue 6 — compact provenance.

Issues 1-5 already compute source validity, constraint scope, source trust,
verification evidence, and retrieval-time conflict detection. This module
verifies that Issue 6 correctly SURFACES that already-computed state,
compactly, in both the structured trace (``TraceEntry``/
``KnowledgeTraceEntry``) and the compact-text render
(``EnrichedContextPack.as_text()``) — without recomputing any of it, without
leaking sensitive paths/URLs, and without a token-budget regression.

Conventions follow test_conflict_detection.py / test_verification_evidence.py:
unit-level tests build ``MemoryNode`` objects directly; a handful of
full-pipeline tests use the shared ``session`` fixture (real temporary
SQLite) through ``RecallService`` and ``UnifiedContextRetrievalService``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from memory_engine.models.domain import (
    ConflictResolutionStatus,
    EnrichedContextPack,
    MemoryKind,
    MemoryNode,
    MemoryNodeCreate,
    MemoryStatus,
    Project,
    ProjectCreate,
    RecallRequest,
    SourceTrust,
    VerificationEvidenceLevel,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.skills.composer import build_provenance
from memory_engine.skills.recall import RecallService

# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _node(
    title: str = "Some memory",
    summary: str = "Some summary.",
    *,
    kind: MemoryKind = MemoryKind.constraint,
    status: MemoryStatus = MemoryStatus.active,
    confidence: float = 0.9,
    importance: float = 0.7,
    trust_level: str | None = SourceTrust.reviewed_committed_design.value,
    evidence_level: str | None = None,
    source_path: str | None = None,
    source_symbol: str | None = None,
    commit_sha: str | None = None,
    branch_name: str | None = None,
    branch_scope: str | None = None,
    constraint_scope: str | None = None,
    validity_reason: str | None = None,
) -> MemoryNode:
    n = MemoryNode(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        parent_id=None,
        title=title,
        summary=summary,
        kind=kind,
        depth=0,
        tags=[],
        status=status,
        confidence=confidence,
        importance=importance,
        created_at=_now(),
        updated_at=_now(),
        evidence=[],
    )
    n.trust_level = trust_level
    n.evidence_level = evidence_level
    n.source_path = source_path
    n.source_symbol = source_symbol
    n.commit_sha = commit_sha
    n.branch_name = branch_name
    n.branch_scope = branch_scope
    n.constraint_scope = constraint_scope
    n.validity_reason = validity_reason
    return n


def _project() -> Project:
    return Project(
        id=uuid.uuid4(), name="prov-project", description=None,
        created_at=_now(), updated_at=_now(),
    )


def _pack_with(node: MemoryNode, prov) -> EnrichedContextPack:
    """Minimal EnrichedContextPack carrying exactly one constraint node plus
    its provenance entry, for as_text() assertions."""
    kwargs = {"project": _project()}
    bucket = {
        MemoryKind.constraint: "constraints",
        MemoryKind.architecture: "architecture",
        MemoryKind.module: "modules",
        MemoryKind.decision: "decisions",
        MemoryKind.debug: "incidents",
        MemoryKind.outcome: "incidents",
        MemoryKind.procedure: "procedures",
    }[node.kind]
    kwargs[bucket] = [node]
    kwargs["provenance"] = {str(node.id): prov} if prov is not None else {}
    return EnrichedContextPack(**kwargs)


# ---------------------------------------------------------------------------
# 1. Active, trusted, verified memory — no spurious non-authoritative markers.
# ---------------------------------------------------------------------------


def test_active_trusted_verified_memory_has_no_spurious_markers():
    node = _node(
        kind=MemoryKind.constraint,
        status=MemoryStatus.active,
        trust_level=SourceTrust.reviewed_committed_design.value,
        evidence_level=VerificationEvidenceLevel.human_confirmed.value,
        constraint_scope="repository",
    )
    prov = build_provenance(node, {"symbol_overlap": 1.0})

    assert prov.status == "active"
    assert prov.authority is None
    assert prov.trust_level == "reviewed-design"
    assert prov.verification_level == "human-confirmed"
    assert prov.matched_by == ["symbol"]

    text = _pack_with(node, prov).as_text()
    assert "UNTRUSTED_REPOSITORY_CONTENT" not in text
    assert "authority:" not in text
    assert "verification: human-confirmed" in text
    assert "trust: reviewed-design" in text
    # Default/active status is not spelled out as a line (compact).
    assert "status: active" not in text


# ---------------------------------------------------------------------------
# 2. stale/needs_revalidation memory — excluded trace entry shows the
#    non-authoritative marker and validity_reason.
# ---------------------------------------------------------------------------


def test_needs_revalidation_memory_shows_non_authoritative_marker():
    node = _node(
        status=MemoryStatus.needs_revalidation,
        validity_reason="source file hash changed since write time",
    )
    prov = build_provenance(node)

    assert prov.status == "needs_revalidation"
    assert prov.authority == "non-authoritative"
    assert prov.validity_reason == "source file hash changed since write time"

    text = _pack_with(node, prov).as_text()
    assert "status: needs_revalidation" in text
    assert "authority: non-authoritative" in text
    assert "validity_reason: source file hash changed since write time" in text


# ---------------------------------------------------------------------------
# 3. Untrusted / low-trust authoritative memory — reuses Issue 3's exact
#    "authority: evidence-only" convention.
# ---------------------------------------------------------------------------


def test_untrusted_authoritative_memory_shows_evidence_only():
    node = _node(kind=MemoryKind.constraint, trust_level=SourceTrust.diff_or_log.value)
    prov = build_provenance(node)

    assert prov.authority == "evidence-only"
    assert prov.trust_level == "diff-or-log"

    text = _pack_with(node, prov).as_text()
    assert "UNTRUSTED_REPOSITORY_CONTENT" in text
    assert "authority: evidence-only" in text
    # Never duplicated — the old marker already said it once.
    assert text.count("authority: evidence-only") == 1


# ---------------------------------------------------------------------------
# 4. Scoped constraint — provenance shows constraint_scope value.
# ---------------------------------------------------------------------------


def test_scoped_constraint_shows_constraint_scope():
    node = _node(kind=MemoryKind.constraint, constraint_scope="module")
    prov = build_provenance(node)

    assert prov.constraint_scope == "module"
    text = _pack_with(node, prov).as_text()
    assert "constraint_scope: module" in text


def test_non_constraint_kind_has_no_constraint_scope():
    node = _node(kind=MemoryKind.module)
    prov = build_provenance(node)
    assert prov.constraint_scope is None


# ---------------------------------------------------------------------------
# 5. Verification level variation — agent_claimed vs human_confirmed differ.
# ---------------------------------------------------------------------------


def test_verification_level_differs_agent_claimed_vs_human_confirmed():
    agent = _node(evidence_level=VerificationEvidenceLevel.agent_claimed.value)
    human = _node(evidence_level=VerificationEvidenceLevel.human_confirmed.value)

    prov_agent = build_provenance(agent)
    prov_human = build_provenance(human)

    assert prov_agent.verification_level == "agent-claimed"
    assert prov_human.verification_level == "human-confirmed"
    assert prov_agent.verification_level != prov_human.verification_level


# ---------------------------------------------------------------------------
# 6 & 7. Branch fallback + conflict status — full pipeline via RecallService.
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(session):
    return ProjectService(session).create(
        ProjectCreate(name="compact-provenance-project", description="Issue 6 fixture")
    )


def _create_decision(session, project, *, title, summary, branch_name=None, branch_scope=None,
                      source_symbol=None, trust_level=SourceTrust.reviewed_committed_design.value):
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title=title,
        summary=summary,
        kind=MemoryKind.decision,
        source_symbol=source_symbol,
        confidence=0.9,
        trust_level=trust_level,
    ))
    if branch_name is not None or branch_scope is not None:
        MemoryNodeRepository(session).update_fields(str(node.id), branch_name=branch_name)
        orm = MemoryNodeRepository(session).get_bare(str(node.id))
        orm.branch_scope = branch_scope
        session.commit()
    return node


def test_branch_fallback_shows_historical_marker(session, project):
    grpc = _create_decision(
        session, project,
        title="Transport protocol", summary="Use gRPC for service communication.",
        source_symbol="TransportConfig", branch_name="feature/grpc", branch_scope="current_branch",
    )
    rest = _create_decision(
        session, project,
        title="Transport protocol", summary="Use REST for service communication.",
        source_symbol="TransportConfig", branch_name="main", branch_scope="mainline",
    )

    result = RecallService(session).recall(RecallRequest(
        project_id=project.id,
        current_task="What transport protocol should services use?",
        current_branch="feature/grpc",
        token_budget=6000,
    ))

    grpc_entry = next(t for t in result.retrieval_trace if t.memory_id == str(grpc.id))
    rest_entry = next(t for t in result.retrieval_trace if t.memory_id == str(rest.id))

    assert grpc_entry.provenance is not None
    assert grpc_entry.provenance.historical is False
    assert rest_entry.provenance is not None
    assert rest_entry.provenance.historical is True

    text = result.context_pack.as_text()
    assert "scope: mainline-fallback" in text


def test_conflict_status_appears_unresolved_and_preferred(session, project):
    # Unresolved: same branch, no supersedes relation.
    a = _create_decision(
        session, project,
        title="Retry policy A", summary="Retries use exponential backoff for payments.",
        source_symbol="RetryConfig", branch_name="feature/x", branch_scope="current_branch",
    )
    b = _create_decision(
        session, project,
        title="Retry policy B", summary="Retries use fixed backoff for payments.",
        source_symbol="RetryConfig", branch_name="feature/x", branch_scope="current_branch",
    )

    result = RecallService(session).recall(RecallRequest(
        project_id=project.id,
        current_task="What retry policy applies to payments?",
        current_branch="feature/x",
        token_budget=6000,
    ))
    a_entry = next(t for t in result.retrieval_trace if t.memory_id == str(a.id))
    assert a_entry.provenance is not None
    assert a_entry.provenance.conflict_status == ConflictResolutionStatus.unresolved.value
    text = result.context_pack.as_text()
    assert "conflict: unresolved" in text

    # Preferred: current-branch vs mainline.
    grpc = _create_decision(
        session, project,
        title="Transport protocol", summary="Use gRPC for service communication.",
        source_symbol="TransportConfig", branch_name="feature/grpc", branch_scope="current_branch",
    )
    _create_decision(
        session, project,
        title="Transport protocol", summary="Use REST for service communication.",
        source_symbol="TransportConfig", branch_name="main", branch_scope="mainline",
    )
    result2 = RecallService(session).recall(RecallRequest(
        project_id=project.id,
        current_task="What transport protocol should services use?",
        current_branch="feature/grpc",
        token_budget=6000,
    ))
    grpc_entry = next(t for t in result2.retrieval_trace if t.memory_id == str(grpc.id))
    assert grpc_entry.provenance.conflict_status == ConflictResolutionStatus.current_branch_preferred.value
    assert grpc_entry.provenance.conflict_alternatives_count == 1
    text2 = result2.context_pack.as_text()
    assert "conflict: current_branch_preferred (alternatives: 1)" in text2


def test_conflict_propagates_through_fusion_to_knowledge_trace_entry(session, project):
    """Closes the Issue 5-flagged gap: fusion.py's memory -> knowledge trace
    conversion previously dropped TraceEntry.conflict entirely."""
    from memory_engine.knowledge.fusion import UnifiedContextRetrievalService
    from memory_engine.models.knowledge_domain import UnifiedRetrievalRequest

    grpc = _create_decision(
        session, project,
        title="Transport protocol", summary="Use gRPC for service communication.",
        source_symbol="TransportConfig", branch_name="feature/grpc", branch_scope="current_branch",
    )
    _create_decision(
        session, project,
        title="Transport protocol", summary="Use REST for service communication.",
        source_symbol="TransportConfig", branch_name="main", branch_scope="mainline",
    )

    svc = UnifiedContextRetrievalService(session, include_knowledge=False) \
        if False else UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=project.id,
        task="What transport protocol should services use?",
        current_branch="feature/grpc",
        token_budget=6000,
        include_knowledge=False,
    )
    pack = svc.retrieve(req)

    grpc_kte = next(
        t for t in pack.retrieval_trace
        if t.result_type == "memory" and t.result_id == str(grpc.id)
    )
    assert grpc_kte.conflict is not None
    assert grpc_kte.conflict.resolution_status == ConflictResolutionStatus.current_branch_preferred
    assert grpc_kte.provenance is not None
    assert grpc_kte.provenance.conflict_status == ConflictResolutionStatus.current_branch_preferred.value


# ---------------------------------------------------------------------------
# 8. Legacy memory with none of the new metadata — graceful degradation.
# ---------------------------------------------------------------------------


def test_legacy_memory_degrades_gracefully():
    node = _node(kind=MemoryKind.constraint, trust_level=None, evidence_level=None,
                 constraint_scope=None, source_path=None, validity_reason=None)
    prov = build_provenance(node)

    assert prov.trust_level == "unknown"
    assert prov.verification_level == "unverified"
    # Legacy constraint with no explicit scope falls back to the existing,
    # conservative constraint_scope.infer_legacy_scope() default
    # ("repository") — never fabricated as "global", never crashes.
    assert prov.constraint_scope == "repository"
    assert prov.source_path is None
    assert prov.validity_reason is None

    # Rendering must not crash and must not invent validity/conflict lines.
    text = _pack_with(node, prov).as_text()
    assert "validity_reason:" not in text
    assert "conflict:" not in text
    assert "constraint_scope: repository" in text


def test_legacy_low_trust_constraint_is_flagged_evidence_only():
    """SourceTrust.unknown is below MIN_AUTHORITATIVE_TRUST, so a legacy
    constraint with NO trust_level at all is correctly (not fabricated)
    treated the same as any other untrusted authoritative content."""
    node = _node(kind=MemoryKind.constraint, trust_level=None)
    prov = build_provenance(node)
    assert prov.trust_level == "unknown"
    assert prov.authority == "evidence-only"


# ---------------------------------------------------------------------------
# 9 & 10. No absolute paths / raw remote URLs ever appear in provenance.
# ---------------------------------------------------------------------------


def test_no_absolute_filesystem_path_in_provenance():
    node = _node(source_path="memory_engine/services/source_trust.py", source_symbol="effective_trust")
    prov = build_provenance(node)
    assert prov.source_path == "memory_engine/services/source_trust.py"
    assert not prov.source_path.startswith("/")

    text = _pack_with(node, prov).as_text()
    assert "/Users/" not in text
    assert "/home/" not in text


def test_no_raw_git_remote_url_in_fingerprint(tmp_path):
    from memory_engine.bootstrap.local_storage import ProjectLocalStorage

    storage = ProjectLocalStorage(tmp_path)
    fp = storage.short_repository_fingerprint()

    assert isinstance(fp, str)
    assert len(fp) == 12
    assert "http" not in fp
    assert "git@" not in fp
    assert str(tmp_path) not in fp
    # Must be a prefix of the full (already-hashed, never-raw) path_hash.
    full = storage._build_fingerprint()["path_hash"]
    assert full.startswith(fp)


# ---------------------------------------------------------------------------
# 11. No cross-project data leakage.
# ---------------------------------------------------------------------------


def test_fingerprint_differs_across_projects(tmp_path):
    from memory_engine.bootstrap.local_storage import ProjectLocalStorage

    a = tmp_path / "project_a"
    b = tmp_path / "project_b"
    a.mkdir()
    b.mkdir()

    fp_a = ProjectLocalStorage(a).short_repository_fingerprint()
    fp_b = ProjectLocalStorage(b).short_repository_fingerprint()
    assert fp_a != fp_b


# ---------------------------------------------------------------------------
# 12. Token-budget sanity — compact provenance stays small.
# ---------------------------------------------------------------------------


def test_provenance_text_is_compact_per_item():
    node = _node(
        kind=MemoryKind.constraint,
        status=MemoryStatus.needs_revalidation,
        trust_level=SourceTrust.diff_or_log.value,
        evidence_level=VerificationEvidenceLevel.agent_claimed.value,
        constraint_scope="module",
        source_path="memory_engine/services/example.py",
        source_symbol="do_thing",
        commit_sha="abcdef1234567890",
        validity_reason="source changed",
    )
    prov = build_provenance(node, {"symbol_overlap": 1.0})
    prov.historical = True
    prov.conflict_status = "unresolved"

    lines = [
        ln for ln in _pack_with(node, prov).as_text().splitlines()
        if ln.strip() and node.title not in ln and "Memory Context" not in ln
        and not ln.startswith("##") and node.summary not in ln
        and "_token estimate" not in ln
    ]
    joined = "\n".join(lines)
    # Well under a paragraph's worth of tokens for a single item's provenance.
    assert len(joined) < 400


def test_batch_provenance_overhead_is_small_fraction_of_budget():
    nodes_and_provs = []
    for i in range(20):
        node = _node(
            title=f"Constraint {i}", kind=MemoryKind.constraint,
            source_path=f"memory_engine/services/mod_{i}.py",
            trust_level=SourceTrust.committed_source_or_test.value,
        )
        nodes_and_provs.append((node, build_provenance(node, {"module_path_overlap": 1.0})))

    pack = EnrichedContextPack(
        project=_project(),
        constraints=[n for n, _ in nodes_and_provs],
        provenance={str(n.id): p for n, p in nodes_and_provs},
    )
    text = pack.as_text()
    # Rough token estimate (4 chars/token, matching composer's own convention).
    total_tokens = len(text) // 4
    assert total_tokens < 2000  # a small fraction of the default 6000 budget


# ---------------------------------------------------------------------------
# 13. Old-style callers unaffected — as_text() output identical when no
#     provenance dict is supplied (the pre-Issue-6 default).
# ---------------------------------------------------------------------------


def test_as_text_unaffected_when_provenance_absent():
    node = _node(kind=MemoryKind.constraint, status=MemoryStatus.needs_revalidation,
                 trust_level=SourceTrust.diff_or_log.value, validity_reason="whatever")
    pack = EnrichedContextPack(project=_project(), constraints=[node])
    text = pack.as_text()

    # Only the pre-existing Issue 3 marker logic may appear; none of the new
    # Issue 6 lines are fabricated when provenance wasn't supplied.
    assert "UNTRUSTED_REPOSITORY_CONTENT" in text
    assert "authority: evidence-only" in text
    assert "validity_reason:" not in text
    assert "status: needs_revalidation" not in text
    assert "conflict:" not in text


def test_trace_entry_and_knowledge_trace_entry_defaults_are_none():
    from memory_engine.models.domain import TraceEntry
    from memory_engine.models.knowledge_domain import KnowledgeTraceEntry

    te = TraceEntry(memory_id="x", title="t", action="selected", reason="r", score=0.5)
    assert te.provenance is None
    assert te.conflict is None

    kte = KnowledgeTraceEntry(result_id="x", title="t")
    assert kte.provenance is None
    assert kte.conflict is None
