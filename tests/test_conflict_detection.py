"""Regression tests for Issue 5 — explicit, retrieval-time conflict detection.

Branch-affinity ranking (Phase 9) changes which memory a task sees first,
but does not tell the agent that two *qualifying* memories actively
disagree (e.g. ``main`` says "use REST", ``feature/grpc`` says "use gRPC").
These tests exercise ``memory_engine/services/conflict_detection.py`` (pure
grouping/resolution logic against in-memory ``MemoryNode`` objects — fast,
precise, no false-positive risk from unrelated gates) plus a handful of
full-pipeline integration tests through ``RecallService``/
``UnifiedContextRetrievalService`` using the shared conftest ``session``
fixture (real temporary SQLite), matching the conventions of
test_source_trust.py / test_verification_evidence.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from memory_engine.models.domain import (
    ConflictResolutionStatus,
    ConstraintScope,
    MemoryKind,
    MemoryNode,
    MemoryNodeCreate,
    MemoryStatus,
    ProjectCreate,
    RecallRequest,
    RelationType,
    SourceTrust,
)
from memory_engine.repositories.relation import RelationRepository
from memory_engine.services.conflict_detection import detect_conflicts
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.skills.recall import RecallService

# ---------------------------------------------------------------------------
# Unit-level helpers — build MemoryNode domain objects directly (not
# persisted), matching test_security_correctness.py's `_make_memory_node`
# convention.
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _node(
    title: str,
    summary: str,
    *,
    kind: MemoryKind = MemoryKind.decision,
    status: MemoryStatus = MemoryStatus.active,
    confidence: float = 0.9,
    importance: float = 0.7,
    trust_level: str | None = SourceTrust.reviewed_committed_design.value,
    source_symbol: str | None = None,
    source_path: str | None = None,
    module_path: str | None = None,
    branch_name: str | None = None,
    branch_scope: str | None = None,
    tags: list[str] | None = None,
) -> MemoryNode:
    n = MemoryNode(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        parent_id=None,
        title=title,
        summary=summary,
        kind=kind,
        depth=0,
        tags=tags or [],
        status=status,
        confidence=confidence,
        importance=importance,
        created_at=_now(),
        updated_at=_now(),
        evidence=[],
    )
    n.trust_level = trust_level
    n.source_symbol = source_symbol
    n.source_path = source_path
    n.module_path = module_path
    n.branch_name = branch_name
    n.branch_scope = branch_scope
    return n


class _Relation:
    """Minimal duck-typed relation object (mirrors MemoryRelationORM's
    source_id/target_id/relation_type attributes) for unit tests that don't
    need a real DB row."""

    def __init__(self, source_id: str, target_id: str, relation_type: str) -> None:
        self.source_id = source_id
        self.target_id = target_id
        self.relation_type = relation_type


# ---------------------------------------------------------------------------
# 1. main vs current-feature-branch decision, same source_symbol -> conflict,
#    feature branch preferred, main flagged historical/fallback.
# ---------------------------------------------------------------------------


def test_current_branch_preferred_over_mainline_same_symbol():
    grpc = _node(
        "Transport protocol", "Use gRPC for all service-to-service communication.",
        source_symbol="TransportConfig", branch_name="feature/grpc", branch_scope="current_branch",
    )
    rest = _node(
        "Transport protocol", "Use REST for all service-to-service communication.",
        source_symbol="TransportConfig", branch_name="main", branch_scope="mainline",
    )

    result = detect_conflicts([grpc, rest], current_branch="feature/grpc")

    assert str(grpc.id) in result
    assert str(rest.id) in result
    grpc_info = result[str(grpc.id)]
    rest_info = result[str(rest.id)]

    assert grpc_info.resolution_status == ConflictResolutionStatus.current_branch_preferred
    assert grpc_info.preferred_scope == "current_branch"
    assert grpc_info.reason == "same_source_symbol"
    assert grpc_info.conflict_group_id == rest_info.conflict_group_id

    assert len(grpc_info.alternatives) == 1
    assert grpc_info.alternatives[0].memory_id == str(rest.id)
    assert grpc_info.alternatives[0].role == "historical"

    assert len(rest_info.alternatives) == 1
    assert rest_info.alternatives[0].role == "preferred"
    assert rest_info.alternatives[0].memory_id == str(grpc.id)


# ---------------------------------------------------------------------------
# 2. Two active decisions on the SAME branch, same identity signal, no
#    supersedes relation -> unresolved, neither hidden.
# ---------------------------------------------------------------------------


def test_same_branch_conflict_is_unresolved_not_score_broken():
    a = _node(
        "Retry policy", "Retries use exponential backoff.",
        module_path="services.retry", branch_name="feature/x", branch_scope="current_branch",
        confidence=0.95,
    )
    b = _node(
        "Retry policy", "Retries use fixed 1-second delay.",
        module_path="services.retry", branch_name="feature/x", branch_scope="current_branch",
        confidence=0.60,
    )

    result = detect_conflicts([a, b], current_branch="feature/x")

    assert result[str(a.id)].resolution_status == ConflictResolutionStatus.unresolved
    assert result[str(b.id)].resolution_status == ConflictResolutionStatus.unresolved
    assert result[str(a.id)].preferred_scope is None
    assert result[str(a.id)].alternatives[0].role == "unresolved_peer"
    assert result[str(b.id)].alternatives[0].role == "unresolved_peer"


# ---------------------------------------------------------------------------
# 3. Current branch vs. an UNRELATED third feature branch (not mainline, not
#    current) -> the unrelated branch's memory is excluded from the group,
#    even though it shares the same identity signal (bounded scope, Issue 5
#    rule 12).
# ---------------------------------------------------------------------------


def test_unrelated_third_branch_not_pulled_into_conflict():
    current = _node(
        "Cache eviction", "Uses LRU eviction.",
        source_symbol="CachePolicy", branch_name="feature/cache-lru", branch_scope="current_branch",
    )
    unrelated = _node(
        "Cache eviction", "Uses LFU eviction.",
        source_symbol="CachePolicy", branch_name="feature/cache-lfu-experiment",
        branch_scope="current_branch",
    )

    result = detect_conflicts([current, unrelated], current_branch="feature/cache-lru")

    # Only two members and one is bounded-out -> no group of size >= 2 forms.
    assert result == {}


# ---------------------------------------------------------------------------
# 4. A superseded alternative does not create an active conflict with the
#    memory that superseded it.
# ---------------------------------------------------------------------------


def test_superseded_alternative_excluded_by_status():
    winner = _node(
        "Auth strategy", "Use JWT for auth.",
        source_path="auth/service.py", status=MemoryStatus.active,
    )
    loser = _node(
        "Auth strategy", "Use session cookies for auth.",
        source_path="auth/service.py", status=MemoryStatus.superseded,
    )

    result = detect_conflicts([winner, loser], current_branch=None)
    assert result == {}


def test_supersedes_relation_defensively_excludes_target_even_if_still_active():
    """Defensive case: both sides still carry status=active (transition not
    yet persisted) but an explicit `supersedes` relation already exists —
    the superseded (target) side must not participate."""
    winner = _node("Auth strategy", "Use JWT for auth.", source_path="auth/service.py")
    loser = _node("Auth strategy", "Use session cookies for auth.", source_path="auth/service.py")

    rel = _Relation(str(winner.id), str(loser.id), RelationType.supersedes.value)
    result = detect_conflicts([winner, loser], current_branch=None, relations=[rel])
    assert result == {}


# ---------------------------------------------------------------------------
# 5. A stale/invalidated (Issue 1) alternative does not create an active
#    conflict.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_status", [
    MemoryStatus.stale, MemoryStatus.invalidated, MemoryStatus.needs_revalidation,
])
def test_stale_or_invalid_alternative_excluded(bad_status):
    active_node = _node("Deploy strategy", "Deploy via blue/green.", module_path="infra.deploy")
    bad_node = _node(
        "Deploy strategy", "Deploy via rolling update.", module_path="infra.deploy",
        status=bad_status,
    )

    result = detect_conflicts([active_node, bad_node], current_branch=None)
    assert result == {}


# ---------------------------------------------------------------------------
# 6. An untrusted (Issue 3, below authority threshold) alternative does not
#    create an active conflict, even if content differs.
# ---------------------------------------------------------------------------


def test_untrusted_alternative_excluded_even_with_different_content():
    trusted = _node(
        "Logging policy", "Never log secrets.", module_path="infra.logging",
        trust_level=SourceTrust.reviewed_committed_design.value,
    )
    untrusted = _node(
        "Logging policy", "Log everything including request bodies.",
        module_path="infra.logging",
        trust_level=SourceTrust.generated_report.value,  # below MIN_AUTHORITATIVE_TRUST
    )

    result = detect_conflicts([trusted, untrusted], current_branch=None)
    assert result == {}


# ---------------------------------------------------------------------------
# 7. Two candidates with a stored explicit `contradicts` relation are
#    grouped even without matching symbol/path/module.
# ---------------------------------------------------------------------------


def test_explicit_contradicts_relation_groups_unrelated_identity_signals():
    a = _node("API versioning", "Use URL-based versioning.", module_path="api.versioning")
    b = _node("Endpoint conventions", "Use header-based versioning.", module_path="api.headers")

    rel = _Relation(str(a.id), str(b.id), RelationType.contradicts.value)
    result = detect_conflicts([a, b], current_branch=None, relations=[rel])

    assert str(a.id) in result and str(b.id) in result
    assert result[str(a.id)].reason == "explicit_contradicts_relation"
    assert result[str(a.id)].conflict_group_id == result[str(b.id)].conflict_group_id


# ---------------------------------------------------------------------------
# 8. Two candidates with merely similar wording but no shared identity
#    signal are NOT falsely flagged as conflicting.
# ---------------------------------------------------------------------------


def test_similar_wording_without_identity_signal_is_not_a_false_positive():
    a = _node(
        "Retry policy for payments", "Payments retry three times with backoff.",
        module_path="payments.retry",
    )
    b = _node(
        "Retry policy for notifications", "Notifications retry three times with backoff.",
        module_path="notifications.retry",
    )

    result = detect_conflicts([a, b], current_branch=None)
    assert result == {}


def test_decision_title_exact_match_still_requires_normalized_equality():
    """Decision-kind fallback grouping requires an exact normalized-title
    match, not fuzzy similarity — slightly different titles never group."""
    a = _node("Use REST for services", "REST everywhere.", kind=MemoryKind.decision)
    b = _node("Use REST for the service layer", "REST everywhere.", kind=MemoryKind.decision)

    result = detect_conflicts([a, b], current_branch=None)
    assert result == {}


# ---------------------------------------------------------------------------
# 9. No branch context (current_branch=None) — conflict detection still
#    works using non-branch identity signals; resolution gracefully
#    degrades to unresolved (documented: no branch-preference signal is
#    available to compute a preference without a current_branch).
# ---------------------------------------------------------------------------


def test_no_current_branch_still_detects_conflict_but_cannot_prefer():
    a = _node("Transport protocol", "Use gRPC.", source_symbol="TransportConfig")
    b = _node("Transport protocol", "Use REST.", source_symbol="TransportConfig")

    result = detect_conflicts([a, b], current_branch=None)

    assert result[str(a.id)].resolution_status == ConflictResolutionStatus.unresolved
    assert result[str(a.id)].preferred_scope is None


# ---------------------------------------------------------------------------
# 10. Non-Git repository / no branch metadata at all on either candidate —
#     no crash, sensible behavior (grouped, unresolved).
# ---------------------------------------------------------------------------


def test_no_branch_metadata_at_all_no_crash_sensible_grouping():
    a = _node("Data retention", "Retain logs for 30 days.", source_path="policy/retention.md")
    b = _node("Data retention", "Retain logs for 90 days.", source_path="policy/retention.md")
    assert a.branch_name is None and b.branch_name is None

    result = detect_conflicts([a, b], current_branch=None)

    assert result[str(a.id)].resolution_status == ConflictResolutionStatus.unresolved


# ---------------------------------------------------------------------------
# 11. conflict_group_id is deterministic across repeated calls with the same
#     inputs.
# ---------------------------------------------------------------------------


def test_conflict_group_id_is_deterministic_across_calls():
    a = _node("Transport protocol", "Use gRPC.", source_symbol="TransportConfig",
              branch_name="feature/grpc", branch_scope="current_branch")
    b = _node("Transport protocol", "Use REST.", source_symbol="TransportConfig",
              branch_name="main", branch_scope="mainline")

    result1 = detect_conflicts([a, b], current_branch="feature/grpc")
    result2 = detect_conflicts([a, b], current_branch="feature/grpc")
    # Also assert order-independence — same set of candidates, different order.
    result3 = detect_conflicts([b, a], current_branch="feature/grpc")

    assert result1[str(a.id)].conflict_group_id == result2[str(a.id)].conflict_group_id
    assert result1[str(a.id)].conflict_group_id == result3[str(a.id)].conflict_group_id


# ---------------------------------------------------------------------------
# 12 & 13. Full-pipeline integration: conflict metadata serializes correctly
# in RecallService's retrieval_trace, and reflects a newly-added conflicting
# memory (cache/revision correctness).
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(session):
    return ProjectService(session).create(
        ProjectCreate(name="conflict-detection-project", description="Issue 5 fixture")
    )


def _create_decision(session, project, *, title, summary, branch_name=None, branch_scope=None,
                      source_symbol=None, module_path=None,
                      trust_level=SourceTrust.reviewed_committed_design.value,
                      confidence=0.9):
    from memory_engine.repositories.memory_node import MemoryNodeRepository

    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title=title,
        summary=summary,
        kind=MemoryKind.decision,
        module_path=module_path,
        source_symbol=source_symbol,
        confidence=confidence,
        trust_level=trust_level,
    ))
    if branch_name is not None or branch_scope is not None:
        MemoryNodeRepository(session).update_fields(
            str(node.id), branch_name=branch_name
        )
        # branch_scope isn't in update_fields' signature — set directly via ORM.
        orm = MemoryNodeRepository(session).get_bare(str(node.id))
        orm.branch_scope = branch_scope
        session.commit()
    return node


def test_conflict_metadata_serializes_in_retrieval_trace(session, project):
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

    svc = RecallService(session)
    req = RecallRequest(
        project_id=project.id,
        current_task="What transport protocol should services use?",
        current_branch="feature/grpc",
        token_budget=6000,
    )
    result = svc.recall(req)

    grpc_entry = next(t for t in result.retrieval_trace if t.memory_id == str(grpc.id))
    rest_entry = next(t for t in result.retrieval_trace if t.memory_id == str(rest.id))

    assert grpc_entry.conflict is not None
    assert grpc_entry.conflict.conflict is True
    assert grpc_entry.conflict.resolution_status == ConflictResolutionStatus.current_branch_preferred
    assert grpc_entry.conflict.preferred_scope == "current_branch"
    assert rest_entry.conflict is not None
    assert rest_entry.conflict.alternatives[0].role == "preferred"

    # Round-trips through model_dump() (JSON-serializable contract).
    dumped = grpc_entry.model_dump()
    assert dumped["conflict"]["conflict_group_id"] == grpc_entry.conflict.conflict_group_id


def test_cache_reflects_newly_added_conflict(session, project, tmp_path):
    """A second, conflicting memory added after the first retrieval must be
    visible on the next retrieve() call — no stale cache masks a newly
    introduced conflict (reuses the memory_revision/cache-key pattern from
    Issues 1/3/4)."""
    from memory_engine.knowledge.cache import SimpleCache
    from memory_engine.knowledge.fusion import UnifiedContextRetrievalService
    from memory_engine.models.knowledge_domain import UnifiedRetrievalRequest

    _create_decision(
        session, project,
        title="Transport protocol", summary="Use REST for service communication.",
        source_symbol="TransportConfig", branch_name="main", branch_scope="mainline",
    )

    cache = SimpleCache()
    svc = UnifiedContextRetrievalService(session, cache=cache, project_root=str(tmp_path))
    req = UnifiedRetrievalRequest(
        project_id=project.id,
        task="What transport protocol should services use?",
        token_budget=6000,
        current_branch="feature/grpc",
        include_knowledge=False,
    )

    pack1 = svc.retrieve(req)
    assert sum(1 for d in pack1.decisions if d.title == "Transport protocol") == 1

    # Adding a second conflicting memory bumps memory_revision via
    # promotion — simulate directly through PostTaskService/PromotionService
    # would be heavier; here we mirror test_source_trust.py's convention of
    # invalidating the cache explicitly (the same contract PromotionService
    # invokes in production) after the DB write.
    _create_decision(
        session, project,
        title="Transport protocol", summary="Use gRPC for service communication.",
        source_symbol="TransportConfig", branch_name="feature/grpc", branch_scope="current_branch",
    )
    cache.invalidate_project(str(project.id))

    pack2 = svc.retrieve(req)
    assert sum(1 for d in pack2.decisions if d.title == "Transport protocol") == 2

    # At least one of the two Transport protocol trace entries must now show
    # the conflict was detected: re-fetch via RecallService directly for a
    # precise, typed assertion (UnifiedContextPack's KnowledgeTraceEntry does
    # not yet propagate the typed ConflictInfo — see Issue 5 report's
    # "Remaining risks").
    recall_result = RecallService(session).recall(RecallRequest(
        project_id=project.id,
        current_task="What transport protocol should services use?",
        current_branch="feature/grpc",
        token_budget=6000,
    ))
    assert any(t.conflict is not None for t in recall_result.retrieval_trace)
