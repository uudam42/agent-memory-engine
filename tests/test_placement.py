"""Tests for PlacementService — Phase 3."""
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from memory_engine.models.orm import Base, MemoryNodeORM
from memory_engine.models.domain import MemoryKind
from memory_engine.services.placement import PlacementService

from tests.helpers import make_candidate, make_project_uuid

PROJECT_ID = str(make_project_uuid())


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add_node(session, *, title, kind, depth, parent_id=None, module_path=None):
    obj = MemoryNodeORM(
        project_id=PROJECT_ID,
        parent_id=parent_id,
        title=title,
        summary=f"Summary of {title}",
        kind=kind,
        depth=depth,
        tags=[],
        status="active",
        confidence=0.9,
        importance=0.5,
        module_path=module_path,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


class TestPlacementArchitecture:
    def test_architecture_no_module_path_goes_to_root(self, session):
        svc = PlacementService(session)
        cand = make_candidate(title="Backend Overall Architecture", kind=MemoryKind.architecture)
        decision = svc.decide(cand, project_id=PROJECT_ID)
        assert decision.intended_depth == 0
        assert decision.parent_id is None

    def test_constraint_goes_to_root(self, session):
        svc = PlacementService(session)
        cand = make_candidate(title="All APIs must be authenticated", kind=MemoryKind.constraint)
        decision = svc.decide(cand, project_id=PROJECT_ID)
        assert decision.intended_depth == 0
        assert decision.parent_id is None


class TestPlacementModuleResolution:
    def test_module_with_matching_path_finds_parent(self, session):
        arch = _add_node(session, title="Scheduler Architecture",
                         kind="architecture", depth=0, module_path="scheduler")
        svc = PlacementService(session)
        cand = make_candidate(title="Job Queue Module", kind=MemoryKind.module,
                              module_path="scheduler.job_queue")
        decision = svc.decide(cand, project_id=PROJECT_ID)
        assert str(decision.parent_id) == arch.id
        assert decision.intended_depth == 1

    def test_module_no_match_falls_back_to_natural_depth(self, session):
        svc = PlacementService(session)
        cand = make_candidate(title="Notification Module", kind=MemoryKind.module,
                              module_path="notifier.email")
        decision = svc.decide(cand, project_id=PROJECT_ID)
        assert decision.intended_depth == 2
        assert decision.parent_id is None


class TestPlacementDecisionUnderModule:
    def test_decision_placed_under_module(self, session):
        arch = _add_node(session, title="Retry Architecture",
                         kind="architecture", depth=0, module_path="retry")
        mod = _add_node(session, title="Retry Logic Module",
                        kind="module", depth=1, parent_id=arch.id, module_path="retry.logic")
        svc = PlacementService(session)
        cand = make_candidate(title="Decision: use exponential backoff",
                              kind=MemoryKind.decision, module_path="retry.logic.backoff")
        decision = svc.decide(cand, project_id=PROJECT_ID)
        assert str(decision.parent_id) == mod.id
        assert decision.intended_depth == 2


class TestPlacementExplicitParent:
    def test_explicit_parent_overrides_path_matching(self, session):
        arch = _add_node(session, title="Auth Architecture",
                         kind="architecture", depth=0, module_path="auth")
        mod = _add_node(session, title="Auth Tokens Module",
                        kind="module", depth=1, parent_id=arch.id, module_path="auth.tokens")
        svc = PlacementService(session)
        cand = make_candidate(title="JWT validation logic", kind=MemoryKind.module,
                              module_path="completely.different.path",
                              proposed_parent_id=uuid.UUID(mod.id))
        decision = svc.decide(cand, project_id=PROJECT_ID)
        assert str(decision.parent_id) == mod.id
        assert "Caller-supplied" in decision.placement_reason
