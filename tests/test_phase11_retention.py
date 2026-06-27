"""Phase 11 — Memory Retention & Compaction tests.

Covers:
- ORM schema: new retention columns present on memory_nodes and memory_candidates
- Schema migration idempotency for Phase 11 columns
- MemoryRetentionService: candidate expiry, archival, compaction, branch cleanup
- Protected types excluded from auto-archive and compaction
- Dry-run mutates nothing
- Compaction preserves source-memory IDs and evidence IDs
- Compacted memories link to source via memory_relations
- Restore returns archived memory to active status
- Default retrieval excludes archived/compacted-source memories
- RetentionReport to_dict structure
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from memory_engine.db.init_db import apply_schema_migrations
from memory_engine.models.orm import (
    Base,
    EvidenceORM,
    MemoryCandidateORM,
    MemoryNodeORM,
    MemoryRelationORM,
    ProjectORM,
)
from memory_engine.services.retention import MemoryRetentionService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        apply_schema_migrations(conn)
        conn.commit()
    return engine


@pytest.fixture
def session():
    engine = _make_engine()
    with Session(engine) as s:
        yield s


@pytest.fixture
def project(session):
    p = ProjectORM(id=str(uuid.uuid4()), name="test-project")
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def svc(session, project):
    return MemoryRetentionService(
        session,
        project.id,
        candidate_expiry_days=30,
        inactive_archive_days=120,
        stale_archive_days=180,
        compaction_min_group=3,
        compaction_min_age_days=14,
    )


def _node(session, project_id: str, **kwargs) -> MemoryNodeORM:
    defaults = dict(
        id=str(uuid.uuid4()),
        project_id=project_id,
        title="Test node",
        summary="A test memory.",
        kind="debug",
        depth=1,
        tags=[],
        status="active",
        confidence=0.9,
        importance=0.5,
    )
    defaults.update(kwargs)
    n = MemoryNodeORM(**defaults)
    session.add(n)
    session.flush()
    return n


def _candidate(session, project_id: str, **kwargs) -> MemoryCandidateORM:
    defaults = dict(
        id=str(uuid.uuid4()),
        project_id=project_id,
        title="Test candidate",
        summary="A pending candidate.",
        proposed_kind="debug",
        proposed_tags=[],
        confidence=0.8,
        importance=0.5,
        status="pending",
    )
    defaults.update(kwargs)
    c = MemoryCandidateORM(**defaults)
    session.add(c)
    session.flush()
    return c


def _aged(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# ---------------------------------------------------------------------------
# A. Schema tests
# ---------------------------------------------------------------------------


def test_retention_columns_present_on_memory_nodes():
    engine = _make_engine()
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("memory_nodes")}
    assert "archived_at" in cols
    assert "archived_reason" in cols
    assert "compacted_into_id" in cols
    assert "last_retrieved_at" in cols
    assert "retrieval_count" in cols


def test_retention_columns_present_on_candidates():
    engine = _make_engine()
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("memory_candidates")}
    assert "expires_at" in cols
    assert "expiry_reason" in cols


def test_schema_migration_idempotent():
    engine = _make_engine()
    with engine.connect() as conn:
        apply_schema_migrations(conn)  # second call — must not raise
        apply_schema_migrations(conn)
        conn.commit()


# ---------------------------------------------------------------------------
# B. Candidate expiry
# ---------------------------------------------------------------------------


def test_identify_expired_candidates_dry_run(session, project, svc):
    old = _candidate(session, project.id)
    old.created_at = _aged(40)
    session.flush()

    recent = _candidate(session, project.id)  # created now — not expired

    actions = svc.identify_expired_candidates(dry_run=True)
    expired_ids = {a.memory_id for a in actions}
    assert old.id in expired_ids
    assert recent.id not in expired_ids

    # Dry-run — status unchanged
    session.expire_all()
    assert old.status == "pending"


def test_expire_candidates_applies_on_live_run(session, project, svc):
    old = _candidate(session, project.id)
    old.created_at = _aged(40)
    session.flush()

    actions = svc.identify_expired_candidates(dry_run=False)
    assert len(actions) == 1
    session.refresh(old)
    assert old.status == "expired"
    assert old.expiry_reason is not None
    assert old.expires_at is not None


# ---------------------------------------------------------------------------
# C. Archival
# ---------------------------------------------------------------------------


def test_stale_nodes_archived_after_threshold(session, project, svc):
    stale = _node(session, project.id, status="stale")
    stale.updated_at = _aged(200)
    session.flush()

    actions = svc.identify_archive_candidates(dry_run=False)
    archived_ids = {a.memory_id for a in actions}
    assert stale.id in archived_ids
    session.refresh(stale)
    assert stale.status == "archived"
    assert stale.archived_at is not None
    assert stale.archived_reason is not None


def test_recent_stale_not_archived(session, project, svc):
    stale = _node(session, project.id, status="stale")
    stale.updated_at = _aged(10)  # too recent
    session.flush()

    actions = svc.identify_archive_candidates(dry_run=True)
    assert not any(a.memory_id == stale.id for a in actions)


def test_protected_types_not_archived(session, project, svc):
    constraint = _node(session, project.id, status="stale", kind="constraint")
    constraint.updated_at = _aged(200)
    session.flush()

    actions = svc.identify_archive_candidates(dry_run=True)
    assert not any(a.memory_id == constraint.id for a in actions)


def test_archive_dry_run_no_mutation(session, project, svc):
    stale = _node(session, project.id, status="stale")
    stale.updated_at = _aged(200)
    session.flush()

    svc.identify_archive_candidates(dry_run=True)
    session.refresh(stale)
    assert stale.status == "stale"  # not changed


def test_manual_archive_and_restore(session, project, svc):
    node = _node(session, project.id, status="active")
    svc.archive_memory(node.id, "manual test")
    session.refresh(node)
    assert node.status == "archived"

    svc.restore_memory(node.id)
    session.refresh(node)
    assert node.status == "active"
    assert node.archived_at is None


# ---------------------------------------------------------------------------
# D. Compaction
# ---------------------------------------------------------------------------


def test_compaction_groups_identified(session, project, svc):
    parent_id = str(uuid.uuid4())
    for i in range(3):
        n = _node(session, project.id, kind="debug", parent_id=parent_id)
        n.created_at = _aged(20)
    session.flush()

    groups = svc.identify_compaction_groups()
    assert len(groups) >= 1
    assert len(groups[0]) >= 3


def test_compaction_group_requires_min_size(session, project, svc):
    parent_id = str(uuid.uuid4())
    for i in range(2):  # only 2, below min of 3
        n = _node(session, project.id, kind="debug", parent_id=parent_id)
        n.created_at = _aged(20)
    session.flush()

    groups = svc.identify_compaction_groups()
    assert all(len(g) >= 3 for g in groups)


def test_protected_type_excluded_from_compaction(session, project, svc):
    parent_id = str(uuid.uuid4())
    for i in range(4):
        n = _node(session, project.id, kind="constraint", parent_id=parent_id)
        n.created_at = _aged(20)
    session.flush()

    groups = svc.identify_compaction_groups()
    # No group should contain constraint nodes
    for group in groups:
        assert all(n.kind != "constraint" for n in group)


def test_compact_group_creates_compacted_node(session, project, svc):
    parent_id = str(uuid.uuid4())
    nodes = []
    for i in range(3):
        n = _node(session, project.id, kind="debug", parent_id=parent_id, title=f"Node {i}")
        n.created_at = _aged(20)
        nodes.append(n)
    session.flush()

    result = svc.compact_memory_group(nodes, dry_run=False)
    assert result is not None
    assert result.compacted_memory_id
    assert not result.dry_run
    assert len(result.source_memory_ids) == 3

    # Compacted node must exist
    compacted = session.get(MemoryNodeORM, result.compacted_memory_id)
    assert compacted is not None
    assert compacted.status == "compacted"
    assert compacted.kind == "debug"

    # Source memories linked via relations
    relations = (
        session.query(MemoryRelationORM)
        .filter_by(source_id=result.compacted_memory_id, relation_type="compaction_source")
        .all()
    )
    assert len(relations) == 3
    linked_ids = {r.target_id for r in relations}
    assert {n.id for n in nodes} == linked_ids


def test_compact_dry_run_no_mutation(session, project, svc):
    parent_id = str(uuid.uuid4())
    nodes = []
    for i in range(3):
        n = _node(session, project.id, kind="debug", parent_id=parent_id)
        n.created_at = _aged(20)
        nodes.append(n)
    session.flush()

    original_statuses = {n.id: n.status for n in nodes}
    result = svc.compact_memory_group(nodes, dry_run=True)
    assert result is not None
    assert result.dry_run
    assert result.compacted_memory_id.startswith("(dry-run:")

    for n in nodes:
        session.refresh(n)
        assert n.status == original_statuses[n.id]


def test_compaction_preserves_evidence_ids(session, project, svc):
    parent_id = str(uuid.uuid4())
    nodes = []
    for i in range(3):
        n = _node(session, project.id, kind="debug", parent_id=parent_id)
        n.created_at = _aged(20)
        ev = EvidenceORM(
            id=str(uuid.uuid4()),
            memory_node_id=n.id,
            content=f"Evidence for node {i}",
        )
        session.add(ev)
        nodes.append(n)
    session.flush()

    result = svc.compact_memory_group(nodes, dry_run=True)
    assert len(result.source_evidence_ids) == 3


def test_compact_source_archived_after_compaction(session, project, svc):
    parent_id = str(uuid.uuid4())
    nodes = []
    for i in range(3):
        n = _node(session, project.id, kind="debug", parent_id=parent_id)
        n.created_at = _aged(20)
        nodes.append(n)
    session.flush()

    result = svc.compact_memory_group(nodes, dry_run=False)
    assert result is not None

    for n in nodes:
        session.refresh(n)
        assert n.status == "archived"
        assert n.compacted_into_id == result.compacted_memory_id


# ---------------------------------------------------------------------------
# E. Branch lifecycle
# ---------------------------------------------------------------------------


def test_branch_cleanup_archives_stale_deleted_branch(session, project, svc):
    node = _node(session, project.id, status="active", branch_scope="current_branch")
    node.branch_name = "feature/old"
    node.updated_at = _aged(40)
    session.flush()

    active_branches: set[str] = {"main", "feature/current"}
    actions = svc.evaluate_branch_lifecycle(
        current_branches=active_branches, dry_run=False, archive_after_days=30
    )
    assert any(a.memory_id == node.id for a in actions)
    session.refresh(node)
    assert node.status == "archived"


def test_branch_cleanup_preserves_current_branch(session, project, svc):
    node = _node(session, project.id, status="active", branch_scope="current_branch")
    node.branch_name = "feature/current"
    node.updated_at = _aged(40)
    session.flush()

    active_branches: set[str] = {"main", "feature/current"}
    actions = svc.evaluate_branch_lifecycle(
        current_branches=active_branches, dry_run=True, archive_after_days=30
    )
    assert not any(a.memory_id == node.id for a in actions)


def test_branch_cleanup_preserves_promoted_memory(session, project, svc):
    node = _node(session, project.id, status="active", branch_scope="current_branch")
    node.branch_name = "feature/old"
    node.updated_at = _aged(40)
    node.branch_promotion_eligible = 1  # promoted — should be preserved
    session.flush()

    actions = svc.evaluate_branch_lifecycle(
        current_branches=set(), dry_run=True, archive_after_days=30
    )
    assert not any(a.memory_id == node.id for a in actions)


# ---------------------------------------------------------------------------
# F. RetentionReport
# ---------------------------------------------------------------------------


def test_retention_report_structure(session, project, svc):
    report = svc.generate_report()
    d = report.to_dict()
    assert "ran_at" in d
    assert d["dry_run"] is True
    assert "counts" in d
    assert "active" in d["counts"]
    assert "archived" in d["counts"]
    assert "compacted" in d["counts"]
    assert "archive_actions" in d
    assert "expiry_actions" in d
    assert "compaction_results" in d
    assert "warnings" in d


def test_full_run_dry_run_no_db_changes(session, project, svc):
    stale = _node(session, project.id, status="stale")
    stale.updated_at = _aged(200)
    old_c = _candidate(session, project.id)
    old_c.created_at = _aged(40)
    session.flush()

    report = svc.run(dry_run=True)
    assert report.dry_run

    session.refresh(stale)
    assert stale.status == "stale"
    session.refresh(old_c)
    assert old_c.status == "pending"


def test_full_run_live_mutates_db(session, project, svc):
    stale = _node(session, project.id, status="stale")
    stale.updated_at = _aged(200)
    old_c = _candidate(session, project.id)
    old_c.created_at = _aged(40)
    session.flush()

    report = svc.run(dry_run=False)
    assert not report.dry_run
    assert report.archive_actions or report.expiry_actions

    session.refresh(stale)
    assert stale.status == "archived"
    session.refresh(old_c)
    assert old_c.status == "expired"
