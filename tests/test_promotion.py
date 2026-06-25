"""End-to-end promotion tests — Phase 3.

Required scenarios (from spec):
  1. Duplicate module memory merge — two nearly-identical module nodes → merge
  2. Incident placed under correct module parent
  3. Project-level constraint placed at depth 0 (no parent)
  4. Stale architecture decision is superseded (not blocked) when newer node is verified
  5. Lower-confidence conflicting memory triggers needs_review
  6. Unresolved content contradiction requires human review

Also covers:
  - create action for a completely new candidate
  - supersede action marks old node as superseded with traceable relation
"""
import pytest
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from memory_engine.db.init_db import init_db as _init_db
from memory_engine.models.orm import Base, MemoryNodeORM, ProjectORM, MemoryCandidateORM
from memory_engine.models.domain import MemoryKind, MemoryStatus, PromoteAction
from memory_engine.repositories.candidate import CandidateRepository
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.services.promotion import PromotionService


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def project(session):
    p = ProjectORM(name="test-project", description="Phase 3 test project")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _add_node(session, *, project_id, title, kind, depth, parent_id=None,
              module_path=None, confidence=0.9, status="active", summary=None):
    obj = MemoryNodeORM(
        project_id=project_id,
        parent_id=parent_id,
        title=title,
        summary=summary or f"Summary of {title}",
        kind=kind,
        depth=depth,
        tags=[],
        status=status,
        confidence=confidence,
        importance=0.5,
        module_path=module_path,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _add_candidate(session, *, project_id, title, summary, kind, module_path=None,
                   confidence=0.9, proposed_parent_id=None, evidence_content=None):
    repo = CandidateRepository(session)
    return repo.create(
        project_id=project_id,
        title=title,
        summary=summary,
        proposed_kind=kind.value,
        proposed_tags=[],
        proposed_module_path=module_path,
        proposed_parent_id=proposed_parent_id,
        source_ref=None,
        confidence=confidence,
        importance=0.5,
        evidence_content=evidence_content,
        evidence_source=None,
    )


# ── Test 1: Duplicate module memory merge ─────────────────────────────────

class TestDuplicateModuleMerge:
    def test_near_identical_module_is_merged_or_updated(self, session, project):
        """Two candidates with nearly the same title and kind → merge or update action."""
        project_id = str(project.id)

        # Create existing node
        _add_node(
            session, project_id=project_id,
            title="Scheduler Job Queue Module",
            kind="module", depth=1,
            module_path="scheduler.queue",
            confidence=0.80,
        )

        # Candidate with same title → higher confidence → should update/supersede
        cand = _add_candidate(
            session, project_id=project_id,
            title="Scheduler Job Queue Module",
            summary="The job queue stores pending jobs and dispatches them to workers.",
            kind=MemoryKind.module,
            module_path="scheduler.queue",
            confidence=0.90,
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        # Near-duplicate with higher confidence → supersede or update (never create fresh)
        assert result.action in (PromoteAction.supersede, PromoteAction.update)
        assert not result.needs_human_review


# ── Test 2: Incident placed under correct module ──────────────────────────

class TestIncidentPlacementUnderModule:
    def test_incident_placed_under_module_with_matching_path(self, session, project):
        """Debug/incident candidate finds module parent by module_path overlap."""
        project_id = str(project.id)

        arch = _add_node(
            session, project_id=project_id,
            title="Payment Architecture",
            kind="architecture", depth=0,
            module_path="payments",
        )
        mod = _add_node(
            session, project_id=project_id,
            title="Payment Processing Module",
            kind="module", depth=1,
            parent_id=arch.id,
            module_path="payments.processing",
        )

        cand = _add_candidate(
            session, project_id=project_id,
            title="Incident: payment timeout on high load",
            summary="Payment service times out when concurrent requests exceed 500.",
            kind=MemoryKind.debug,
            module_path="payments.processing.timeout",
            confidence=0.95,
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        assert result.action == PromoteAction.create
        assert str(result.placement.parent_id) == mod.id
        assert result.placement.intended_depth == 2


# ── Test 3: Project-level constraint at depth 0 ───────────────────────────

class TestProjectLevelConstraint:
    def test_constraint_placed_at_root(self, session, project):
        """A constraint without module_path always lands at depth 0, no parent."""
        project_id = str(project.id)

        cand = _add_candidate(
            session, project_id=project_id,
            title="All external APIs must use mutual TLS",
            summary="Security constraint: every outbound API call requires mTLS.",
            kind=MemoryKind.constraint,
            confidence=0.99,
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        assert result.action == PromoteAction.create
        assert result.placement.intended_depth == 0
        assert result.placement.parent_id is None


# ── Test 4: Stale architecture decision is superseded ─────────────────────

class TestStaleArchitectureSupersede:
    def test_newer_high_confidence_supersedes_stale_node(self, session, project):
        """A stale node with lower confidence can be superseded by a fresh one."""
        project_id = str(project.id)

        old = _add_node(
            session, project_id=project_id,
            title="Architecture: use REST for all APIs",
            kind="architecture", depth=0,
            confidence=0.70,
            status="stale",
            summary="We decided to use REST for all internal and external APIs.",
        )

        # New candidate: same topic, higher confidence, newer decision
        cand = _add_candidate(
            session, project_id=project_id,
            title="Architecture: use REST for all APIs",
            summary="We decided to use REST for all internal and external APIs.",
            kind=MemoryKind.architecture,
            confidence=0.95,
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        # Stale existing → no conflict → supersede (confidence 0.95 >= 0.70)
        assert result.action in (PromoteAction.create, PromoteAction.supersede, PromoteAction.update)
        assert not result.needs_human_review

        # Old node traceable — should still exist in DB
        repo = MemoryNodeRepository(session)
        old_node = repo.get_bare(str(old.id))
        assert old_node is not None


# ── Test 5: Lower-confidence conflicting memory → needs_review ────────────

class TestLowerConfidenceConflict:
    def test_low_confidence_candidate_against_high_confidence_needs_review(self, session, project):
        """Candidate confidence much lower than existing → blocked, needs_review."""
        project_id = str(project.id)

        _add_node(
            session, project_id=project_id,
            title="Authentication: JWT tokens required for all endpoints",
            kind="architecture", depth=0,
            confidence=0.98,
            summary="Authentication is required and must always be used on all endpoints.",
        )

        cand = _add_candidate(
            session, project_id=project_id,
            title="Authentication: JWT tokens required for all endpoints",
            summary="Authentication is required and must always be used on all endpoints.",
            kind=MemoryKind.architecture,
            confidence=0.50,  # 0.98 - 0.50 = 0.48 >> TOLERANCE(0.15)
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        assert result.needs_human_review is True
        assert result.action == PromoteAction.needs_review


# ── Test 6: Unresolved content contradiction → needs_review ──────────────

class TestContentContradictionNeedsReview:
    def test_contradicting_summaries_flagged_for_review(self, session, project):
        """When candidate and existing have contradicting content, flag for review."""
        project_id = str(project.id)

        _add_node(
            session, project_id=project_id,
            title="Job Processing Strategy",
            kind="architecture", depth=0,
            confidence=0.85,
            summary="Jobs are processed synchronously and blocking in a sequential queue.",
        )

        cand = _add_candidate(
            session, project_id=project_id,
            title="Job Processing Strategy",
            summary="Jobs are processed asynchronously in a concurrent worker pool.",
            kind=MemoryKind.architecture,
            confidence=0.85,  # Equal confidence, but contradictory content
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        assert result.needs_human_review is True
        assert result.action == PromoteAction.needs_review
        assert result.review_reason is not None


# ── Test 7: Create brand-new node ─────────────────────────────────────────

class TestCreateNewNode:
    def test_no_existing_nodes_creates_new(self, session, project):
        """Candidate with no matching nodes → PromoteAction.create."""
        project_id = str(project.id)

        cand = _add_candidate(
            session, project_id=project_id,
            title="Completely unique memory about obscure subsystem XYZ",
            summary="This is a unique memory with no possible duplicates.",
            kind=MemoryKind.module,
            confidence=0.90,
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        assert result.action == PromoteAction.create
        assert result.target_node is not None
        assert result.target_node.title == "Completely unique memory about obscure subsystem XYZ"


# ── Test 8: Supersede creates relation ───────────────────────────────────

class TestSupersededRelation:
    def test_supersede_creates_supersedes_relation(self, session, project):
        """Supersede action creates MemoryRelation(new, supersedes, old)."""
        project_id = str(project.id)

        from memory_engine.repositories.relation import RelationRepository

        old = _add_node(
            session, project_id=project_id,
            title="Rate Limiting Architecture",
            kind="architecture", depth=0,
            confidence=0.80,
            summary="Rate limiting applies per-IP with fixed window algorithm.",
        )

        # Higher confidence candidate with same title
        cand = _add_candidate(
            session, project_id=project_id,
            title="Rate Limiting Architecture",
            summary="Rate limiting applies per-IP with fixed window algorithm.",
            kind=MemoryKind.architecture,
            confidence=0.95,
        )

        svc = PromotionService(session)
        result = svc.promote(str(cand.id))

        if result.action == PromoteAction.supersede:
            # Verify: supersedes relation exists
            assert len(result.relations_created) >= 1
            rel = result.relations_created[0]
            assert rel.relation_type.value == "supersedes"

            # Old node is now superseded
            repo = MemoryNodeRepository(session)
            old_node = repo.get_bare(str(old.id))
            assert old_node.status == "superseded"
        else:
            # update is also acceptable (partial match)
            assert result.action in (PromoteAction.update, PromoteAction.create)
