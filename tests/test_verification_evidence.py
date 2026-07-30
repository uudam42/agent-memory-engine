"""Regression tests for Issue 4 — verification evidence levels.

An agent's own post-task report (``ReflectionInput.verification_status``)
must not be treated as an independently verified fact. This module tests
``memory_engine/services/verification_evidence.py`` and its integration into
ReflectionSkill -> PostTaskService -> PromotionService, following the exact
conventions used by test_source_trust.py / test_source_validity.py (real
temporary SQLite via the shared conftest `session` fixture, plus a dedicated
legacy-migration test using a real on-disk SQLite file).
"""

from __future__ import annotations

import pytest

from memory_engine.models.domain import (
    MemoryKind,
    MemoryNode,
    MemoryNodeCreate,
    MemoryStatus,
    ProjectCreate,
    ReflectionInput,
    TaskIntent,
    TaskOutcome,
    VerificationEvidence,
    VerificationEvidenceLevel,
    VerificationStatus,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.post_task import PostTaskService
from memory_engine.services.project_service import ProjectService
from memory_engine.services.promotion import PromotionService
from memory_engine.services.verification_evidence import (
    adjusted_confidence,
    apply_verification_transition,
    assign_creation_evidence_level,
    effective_evidence_level,
    evidence_rank,
    legacy_status_to_evidence_level,
)


@pytest.fixture()
def project(session):
    return ProjectService(session).create(
        ProjectCreate(name="verification-evidence-project", description="Issue 4 fixture")
    )


def _base_reflection_input(project_id, **overrides) -> ReflectionInput:
    defaults = dict(
        project_id=project_id,
        task_description="Fix a null pointer crash in the widget renderer",
        task_outcome=TaskOutcome.completed,
        outcome_summary="Fixed a crash caused by an uninitialized widget cache entry.",
        verification_status=VerificationStatus.tests_passed,
        task_intent=TaskIntent.bug_fix,
    )
    defaults.update(overrides)
    return ReflectionInput(**defaults)


def _first_node_of_kind(session, project, kind):
    return next(
        n for n in MemoryService(session).list_by_project(str(project.id))
        if n.kind == kind
    )


# ---------------------------------------------------------------------------
# 1-3. Legacy claims map to agent_claimed, never something stronger.
# ---------------------------------------------------------------------------


def test_legacy_tests_passed_maps_to_agent_claimed(session, project):
    inp = _base_reflection_input(project.id, verification_status=VerificationStatus.tests_passed)
    result = PostTaskService(session).reflect_and_write(inp)
    assert result.candidates_promoted >= 1

    node = _first_node_of_kind(session, project, MemoryKind.debug)
    assert node.evidence_level == VerificationEvidenceLevel.agent_claimed.value
    assert effective_evidence_level(node) == VerificationEvidenceLevel.agent_claimed


def test_legacy_build_success_maps_to_agent_claimed(session, project):
    inp = _base_reflection_input(project.id, verification_status=VerificationStatus.build_success)
    result = PostTaskService(session).reflect_and_write(inp)
    assert result.candidates_promoted >= 1

    node = _first_node_of_kind(session, project, MemoryKind.debug)
    assert node.evidence_level == VerificationEvidenceLevel.agent_claimed.value


def test_agent_claim_with_zero_structured_evidence_stays_agent_claimed(session, project):
    """An agent claim with no VerificationEvidence attached at all is still
    agent_claimed — the weakest positive claim, never unverified (a real
    claim was made) and never stronger (no evidence backs it)."""
    inp = _base_reflection_input(
        project.id,
        verification_status=VerificationStatus.manual_check,
        verification_evidence=None,
    )
    result = PostTaskService(session).reflect_and_write(inp)
    node = _first_node_of_kind(session, project, MemoryKind.debug)
    assert node.evidence_level == VerificationEvidenceLevel.agent_claimed.value
    assert node.verification_evidence is None


def test_unverified_status_maps_to_unverified_level():
    assert (
        legacy_status_to_evidence_level(VerificationStatus.unverified)
        == VerificationEvidenceLevel.unverified
    )
    assert (
        legacy_status_to_evidence_level(VerificationStatus.tests_failed)
        == VerificationEvidenceLevel.unverified
    )


# ---------------------------------------------------------------------------
# 4-5. Stronger levels require explicit assertion + structured evidence
# through the correct API — never a side effect of wording.
# ---------------------------------------------------------------------------


def test_asserted_level_without_evidence_is_ignored():
    """An asserted stronger level with no VerificationEvidence attached must
    not be honored — this is the guard against a claimed-only path magically
    becoming stronger."""
    level = assign_creation_evidence_level(
        verification_status=VerificationStatus.tests_passed,
        asserted_level=VerificationEvidenceLevel.external_observed,
        evidence=None,
    )
    assert level == VerificationEvidenceLevel.agent_claimed


def test_explicit_external_observed_with_structured_evidence_is_honored(session, project):
    evidence = VerificationEvidence(
        target="pytest tests/",
        exit_code=0,
        output_digest="abc123",
        observer="ci:github-actions",
        external_ref="https://ci.example.com/runs/42",
    )
    inp = _base_reflection_input(
        project.id,
        asserted_evidence_level=VerificationEvidenceLevel.external_observed,
        verification_evidence=evidence,
    )
    result = PostTaskService(session).reflect_and_write(inp)
    assert result.candidates_promoted >= 1
    node = _first_node_of_kind(session, project, MemoryKind.debug)
    assert node.evidence_level == VerificationEvidenceLevel.external_observed.value


def test_explicit_engine_observed_with_structured_evidence_is_honored(session, project):
    """engine_observed is documented as currently unreachable via any
    production execution path (Issue 4 forbids adding one this session), but
    the explicit-assertion API itself must still function correctly for a
    hypothetical trusted caller / future feature to build on."""
    evidence = VerificationEvidence(target="pytest tests/", exit_code=0)
    inp = _base_reflection_input(
        project.id,
        asserted_evidence_level=VerificationEvidenceLevel.engine_observed,
        verification_evidence=evidence,
    )
    result = PostTaskService(session).reflect_and_write(inp)
    node = _first_node_of_kind(session, project, MemoryKind.debug)
    assert node.evidence_level == VerificationEvidenceLevel.engine_observed.value


def test_external_observed_evidence_stored_and_retrievable_with_ci_reference(session, project):
    evidence = VerificationEvidence(
        target="pytest tests/",
        exit_code=0,
        observer="ci:github-actions",
        external_ref="https://ci.example.com/runs/99",
    )
    inp = _base_reflection_input(
        project.id,
        asserted_evidence_level=VerificationEvidenceLevel.external_observed,
        verification_evidence=evidence,
    )
    PostTaskService(session).reflect_and_write(inp)
    node = _first_node_of_kind(session, project, MemoryKind.debug)

    nodes_repo = MemoryNodeRepository(session)
    reloaded = MemoryNode.model_validate(nodes_repo.get_bare(str(node.id)))
    assert reloaded.verification_evidence is not None
    assert reloaded.verification_evidence.external_ref == "https://ci.example.com/runs/99"
    assert reloaded.verification_evidence.observer == "ci:github-actions"


# ---------------------------------------------------------------------------
# 6. human_confirmed only reachable via explicit, auditable elevation.
# ---------------------------------------------------------------------------


def test_asserted_human_confirmed_at_creation_is_never_honored(session, project):
    evidence = VerificationEvidence(target="pytest tests/", exit_code=0)
    inp = _base_reflection_input(
        project.id,
        asserted_evidence_level=VerificationEvidenceLevel.human_confirmed,
        verification_evidence=evidence,
    )
    PostTaskService(session).reflect_and_write(inp)
    node = _first_node_of_kind(session, project, MemoryKind.debug)
    assert node.evidence_level != VerificationEvidenceLevel.human_confirmed.value
    assert node.evidence_level == VerificationEvidenceLevel.agent_claimed.value


def test_human_confirmed_reachable_only_via_explicit_elevation(session, project):
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Verified fix",
        summary="A bug fix pending human sign-off.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.agent_claimed.value,
    ))
    assert effective_evidence_level(node) == VerificationEvidenceLevel.agent_claimed

    promo = PromotionService(session)
    updated = promo.set_evidence_level(
        str(node.id),
        new_level=VerificationEvidenceLevel.human_confirmed.value,
        actor="alice@example.com",
        reason="Manually re-ran the test suite and confirmed the fix.",
    )
    assert updated.evidence_level == VerificationEvidenceLevel.human_confirmed.value
    assert effective_evidence_level(updated) == VerificationEvidenceLevel.human_confirmed


# ---------------------------------------------------------------------------
# 7. Workspace isolation — evidence cannot reference the wrong project.
# ---------------------------------------------------------------------------


def test_evidence_is_scoped_to_the_owning_node_project(session, project):
    """VerificationEvidence carries no project_id of its own — it is stored
    directly on a MemoryNode, which is already project-isolated by
    MemoryNodeRepository.list_by_project. There is no code path by which
    evidence attached while creating a node in project A could be attributed
    to project B; this test documents/pins that structural guarantee."""
    other_project = ProjectService(session).create(
        ProjectCreate(name="verification-evidence-other-project")
    )
    evidence = VerificationEvidence(target="pytest tests/", exit_code=0)
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Project-scoped verified node",
        summary="Evidence must stay bound to this project's node only.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.external_observed.value,
        verification_evidence=evidence,
    ))
    project_nodes = MemoryService(session).list_by_project(str(project.id))
    other_nodes = MemoryService(session).list_by_project(str(other_project.id))
    assert any(n.id == node.id for n in project_nodes)
    assert not any(n.id == node.id for n in other_nodes)


# ---------------------------------------------------------------------------
# 8-9. Staleness: branch/commit mismatch downgrades the effective level.
# ---------------------------------------------------------------------------


def test_evidence_from_different_branch_is_downgraded(session, project):
    evidence = VerificationEvidence(
        target="pytest tests/", exit_code=0, source_branch="feature/old-work"
    )
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Branch-bound verification",
        summary="Verified on a branch that has since changed.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.external_observed.value,
        verification_evidence=evidence,
    ))
    # Same branch context -> full strength.
    assert (
        effective_evidence_level(node, current_branch="feature/old-work")
        == VerificationEvidenceLevel.external_observed
    )
    # Different branch context -> downgraded, not deleted.
    assert (
        effective_evidence_level(node, current_branch="feature/new-work")
        == VerificationEvidenceLevel.agent_claimed
    )
    # Stored record is untouched (auditable — no destructive mutation).
    nodes_repo = MemoryNodeRepository(session)
    reloaded = MemoryNode.model_validate(nodes_repo.get_bare(str(node.id)))
    assert reloaded.evidence_level == VerificationEvidenceLevel.external_observed.value


def test_evidence_tied_to_old_commit_is_downgraded_when_head_moves(session, project):
    evidence = VerificationEvidence(
        target="pytest tests/", exit_code=0, source_commit="aaaaaaa"
    )
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Commit-bound verification",
        summary="Verified against a commit that HEAD has since moved past.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.external_observed.value,
        verification_evidence=evidence,
    ))
    assert (
        effective_evidence_level(node, current_commit="aaaaaaa")
        == VerificationEvidenceLevel.external_observed
    )
    assert (
        effective_evidence_level(node, current_commit="bbbbbbb")
        == VerificationEvidenceLevel.agent_claimed
    )


# ---------------------------------------------------------------------------
# 10. Staleness via Issue 1 source-validity integration.
# ---------------------------------------------------------------------------


def test_evidence_becomes_stale_when_source_validity_transitions(session, project):
    """When the node's own source-validity status (Issue 1) has already
    moved to needs_revalidation/invalidated, old verification evidence must
    not keep being read at full strength — this is the 'a memory whose
    source becomes invalid should not keep citing old verification as
    current' hook. Working-tree-content-level staleness (hashing the exact
    files a verification run covered) is NOT wired in this session —
    documented in the module docstring of verification_evidence.py — only
    branch/commit and Issue-1 status transitions are used."""
    evidence = VerificationEvidence(target="pytest tests/", exit_code=0)
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Source-backed verified node",
        summary="Verified while its source file still existed.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.external_observed.value,
        verification_evidence=evidence,
    ))
    assert effective_evidence_level(node) == VerificationEvidenceLevel.external_observed

    nodes_repo = MemoryNodeRepository(session)
    nodes_repo.set_validity(
        str(node.id),
        new_status=MemoryStatus.needs_revalidation.value,
        reason="source content changed since memory was written",
    )
    reloaded = MemoryNode.model_validate(nodes_repo.get_bare(str(node.id)))
    assert effective_evidence_level(reloaded) == VerificationEvidenceLevel.agent_claimed

    nodes_repo.set_validity(
        str(node.id),
        new_status=MemoryStatus.invalidated.value,
        reason="source file no longer exists",
    )
    reloaded = MemoryNode.model_validate(nodes_repo.get_bare(str(node.id)))
    assert effective_evidence_level(reloaded) == VerificationEvidenceLevel.unverified


# ---------------------------------------------------------------------------
# 11. Confidence differs measurably between agent_claimed and human_confirmed.
# ---------------------------------------------------------------------------


def test_confidence_differs_between_agent_claimed_and_human_confirmed(session, project):
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Confidence comparison node",
        summary="Same confidence, different verification-evidence level.",
        kind=MemoryKind.debug,
        confidence=0.8,
        evidence_level=VerificationEvidenceLevel.agent_claimed.value,
    ))
    claimed_confidence = adjusted_confidence(node)

    promo = PromotionService(session)
    elevated = promo.set_evidence_level(
        str(node.id),
        new_level=VerificationEvidenceLevel.human_confirmed.value,
        actor="bob@example.com",
        reason="Manually verified end-to-end.",
    )
    confirmed_confidence = adjusted_confidence(elevated)

    assert confirmed_confidence > claimed_confidence
    # Never large enough to trivially cross a full point of confidence.
    assert confirmed_confidence - claimed_confidence < 0.2


def test_evidence_rank_orders_levels_correctly():
    ordered = [
        VerificationEvidenceLevel.unverified,
        VerificationEvidenceLevel.agent_claimed,
        VerificationEvidenceLevel.external_observed,
        VerificationEvidenceLevel.engine_observed,
        VerificationEvidenceLevel.human_confirmed,
    ]
    ranks = [evidence_rank(lvl) for lvl in ordered]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


# ---------------------------------------------------------------------------
# 12. Backward-compatible serialization for legacy/nullable records.
# ---------------------------------------------------------------------------


def test_legacy_node_with_no_evidence_columns_defaults_to_unverified(session, project):
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Pre-Issue-4 legacy node",
        summary="Existed before evidence_level was introduced.",
        kind=MemoryKind.debug,
    ))
    nodes_repo = MemoryNodeRepository(session)
    orm = nodes_repo.get_bare(str(node.id))
    orm.evidence_level = None
    orm.verification_evidence = None
    session.add(orm)
    session.commit()

    reloaded = MemoryNode.model_validate(nodes_repo.get_bare(str(node.id)))
    assert reloaded.evidence_level is None
    assert effective_evidence_level(reloaded) == VerificationEvidenceLevel.unverified


def test_legacy_boolean_style_flags_map_deterministically():
    """Pins the exact required mapping for the two legacy claim shapes named
    in the Issue 4 spec: tests_passed=True -> agent_claimed,
    build_success=True -> agent_claimed, neither set -> unverified."""
    assert (
        legacy_status_to_evidence_level(VerificationStatus.tests_passed)
        == VerificationEvidenceLevel.agent_claimed
    )
    assert (
        legacy_status_to_evidence_level(VerificationStatus.build_success)
        == VerificationEvidenceLevel.agent_claimed
    )
    assert (
        legacy_status_to_evidence_level(VerificationStatus.unverified)
        == VerificationEvidenceLevel.unverified
    )


# ---------------------------------------------------------------------------
# 13. Migration test — hand-built pre-Issue-4 SQLite schema migrates cleanly.
# ---------------------------------------------------------------------------


def test_legacy_sqlite_database_with_no_evidence_columns_migrates_safely(tmp_path):
    import sqlite3
    import uuid as uuid_mod

    from sqlalchemy import create_engine, text

    db_path = tmp_path / "legacy_pre_evidence.db"
    raw = sqlite3.connect(str(db_path))
    raw.executescript(
        """
        CREATE TABLE projects (
            id VARCHAR(36) PRIMARY KEY,
            name VARCHAR(128) NOT NULL UNIQUE,
            description TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE memory_nodes (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            parent_id VARCHAR(36),
            title VARCHAR(256) NOT NULL,
            summary TEXT NOT NULL,
            kind VARCHAR(32) NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            tags TEXT,
            created_at DATETIME,
            updated_at DATETIME,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            confidence FLOAT NOT NULL DEFAULT 1.0,
            importance FLOAT NOT NULL DEFAULT 0.5,
            module_path VARCHAR(512)
        );
        CREATE TABLE evidence (
            id VARCHAR(36) PRIMARY KEY,
            memory_node_id VARCHAR(36) NOT NULL,
            content TEXT NOT NULL,
            source VARCHAR(512),
            created_at DATETIME
        );
        CREATE TABLE memory_candidates (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            title VARCHAR(256) NOT NULL,
            summary TEXT NOT NULL,
            proposed_kind VARCHAR(32) NOT NULL,
            proposed_tags TEXT,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            confidence FLOAT NOT NULL DEFAULT 0.8,
            importance FLOAT NOT NULL DEFAULT 0.5,
            created_at DATETIME,
            updated_at DATETIME
        );
        """
    )

    project_id = str(uuid_mod.uuid4())
    node_id = str(uuid_mod.uuid4())
    raw.execute(
        "INSERT INTO projects (id, name, description, created_at, updated_at) "
        "VALUES (?, 'legacy-pre-evidence-project', NULL, '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (project_id,),
    )
    raw.execute(
        "INSERT INTO memory_nodes (id, project_id, parent_id, title, summary, kind, depth, tags, "
        "status, confidence, importance, module_path, created_at, updated_at) "
        "VALUES (?, ?, NULL, 'Legacy verified fix', 'pre-Issue-4 row', 'debug', 0, '[]', "
        "'active', 0.9, 0.8, NULL, '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (node_id, project_id),
    )
    raw.commit()
    raw.close()

    from memory_engine.db.init_db import apply_schema_migrations, create_fts_tables

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        create_fts_tables(conn)
        apply_schema_migrations(conn)  # must not raise; adds evidence_* columns
        conn.commit()

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT evidence_level, verification_evidence, previous_evidence_level "
                "FROM memory_nodes WHERE id=:id"
            ),
            {"id": node_id},
        ).fetchone()
    assert row is not None
    assert row[0] is None  # evidence_level column added, but legacy row stays NULL

    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)
    s = Session()
    orm = MemoryNodeRepository(s).get_bare(node_id)
    node = MemoryNode.model_validate(orm)
    assert effective_evidence_level(node) == VerificationEvidenceLevel.unverified
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 14. Audit trail — verification-level changes are real, DB-backed, and tested.
# ---------------------------------------------------------------------------


def test_verification_evidence_audit_trail_recorded_in_db(session, project):
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Audited verification node",
        summary="Evidence-level changes must be recorded, not just returned.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.agent_claimed.value,
    ))
    nodes_repo = MemoryNodeRepository(session)
    updated = apply_verification_transition(
        nodes_repo,
        node,
        new_level=VerificationEvidenceLevel.human_confirmed,
        actor="carol@example.com",
        reason="Manually re-ran the full suite and confirmed the outcome.",
    )
    assert updated is not None

    # Real DB-backed check — re-fetch the row directly, not the in-memory
    # return value, mirroring test_trust_audit_trail_recorded_in_db.
    reloaded_orm = nodes_repo.get_bare(str(node.id))
    reloaded = MemoryNode.model_validate(reloaded_orm)
    assert reloaded.evidence_level == VerificationEvidenceLevel.human_confirmed.value
    assert reloaded.previous_evidence_level == VerificationEvidenceLevel.agent_claimed.value
    assert reloaded.evidence_elevated_by == "carol@example.com"
    assert reloaded.evidence_elevated_reason == "Manually re-ran the full suite and confirmed the outcome."
    assert reloaded.evidence_elevated_at is not None
    assert reloaded.evidence_set_at is not None
    assert "carol@example.com" in (reloaded.evidence_reason or "")


def test_no_op_transition_is_not_recorded(session, project):
    """Mirrors apply_trust_transition's no-op guarantee: requesting the same
    level as the current effective level records nothing fabricated."""
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="No-op transition node",
        summary="Requesting the same level twice must not fabricate a transition.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.agent_claimed.value,
    ))
    nodes_repo = MemoryNodeRepository(session)
    result = apply_verification_transition(
        nodes_repo,
        node,
        new_level=VerificationEvidenceLevel.agent_claimed,
        actor="dave@example.com",
        reason="No real change.",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Task 9 (integration review) — head_commit is never threaded from
# RecallRequest into build_provenance()/effective_evidence_level(), so the
# commit-based staleness downgrade proven by
# test_evidence_tied_to_old_commit_is_downgraded_when_head_moves above (which
# calls effective_evidence_level() directly) never actually fires during a
# real recall()/retrieve() call — current_branch is threaded end-to-end but
# current_commit is not. This reproduces the gap at the composer/build_
# provenance layer (the actual conversion point RecallService relies on) and
# at the full RecallService.recall() layer (the real production path).
# ---------------------------------------------------------------------------


def test_build_provenance_downgrades_verification_on_commit_drift(session, project):
    """build_provenance() must accept and honor a current_commit parameter,
    mirroring its existing current_branch parameter, so a memory verified
    against a commit HEAD has since moved past is displayed as
    agent_claimed rather than stale human_confirmed/external_observed."""
    from memory_engine.skills.composer import build_provenance

    evidence = VerificationEvidence(
        target="pytest tests/", exit_code=0, source_commit="aaaaaaa"
    )
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Commit-bound verification (provenance)",
        summary="Verified against a commit that HEAD has since moved past.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.human_confirmed.value,
        verification_evidence=evidence,
    ))

    same_commit_prov = build_provenance(node, current_commit="aaaaaaa")
    assert same_commit_prov.verification_level == "human-confirmed"

    moved_commit_prov = build_provenance(node, current_commit="bbbbbbb")
    assert moved_commit_prov.verification_level == "agent-claimed", (
        "build_provenance() did not downgrade verification_level on commit "
        "drift — current_commit is not being forwarded to "
        "effective_evidence_level()."
    )


def test_recall_request_threads_current_commit_to_provenance(session, project):
    """End-to-end: RecallRequest must expose a current_commit field that
    RecallService.recall() forwards all the way to each trace entry's
    CompactProvenance, exactly like the existing current_branch field."""
    from memory_engine.models.domain import RecallRequest
    from memory_engine.skills.recall import RecallService

    evidence = VerificationEvidence(
        target="pytest tests/", exit_code=0, source_commit="aaaaaaa"
    )
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Commit-bound verification (recall)",
        summary="Verified against a commit that HEAD has since moved past.",
        kind=MemoryKind.debug,
        evidence_level=VerificationEvidenceLevel.human_confirmed.value,
        verification_evidence=evidence,
    ))

    svc = RecallService(session)
    req = RecallRequest(
        project_id=project.id,
        current_task="Commit-bound verification recall",
        current_commit="bbbbbbb",
        token_budget=6000,
    )
    result = svc.recall(req)
    entry = next(t for t in result.retrieval_trace if t.memory_id == str(node.id))
    assert entry.provenance is not None
    assert entry.provenance.verification_level == "agent-claimed", (
        "RecallRequest.current_commit was not threaded through to "
        "CompactProvenance.verification_level."
    )
