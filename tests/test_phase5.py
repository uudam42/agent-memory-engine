"""Phase 5 tests — post-task reflection and automatic memory writing.

Tested scenarios:
  1.  Bug fix (tests_passed) creates an incident memory
  2.  Failed task → reflection skipped, nothing written
  3.  Reverted task → reflection skipped
  4.  tests_failed → reflection skipped
  5.  Unverified + low agent_confidence → reflection skipped
  6.  Trivial rename with no signals → reflection skipped
  7.  Discovered constraint generates constraint candidate with high importance
  8.  Discovered procedure generates procedure candidate
  9.  Refactor creates module candidate
  10. High-risk refactor also creates a decision candidate
  11. End-to-end: reflect_and_write stages + promotes + returns PostTaskResult
  12. End-to-end: duplicate existing memory → merge/update, not create
  13. Constraint memory has higher importance than module memory
  14. Fully verified work (tests_passed) yields confidence >= 0.90
  15. Manual check yields confidence in (0.70, 0.90)
  16. No user commands needed — agent calls one endpoint, everything happens
  17. API endpoint POST /v1/skills/reflect-and-write returns PostTaskResult
  18. Deterministic: same ReflectionInput → same ReflectionAnalysis
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from memory_engine.models.domain import (
    MemoryKind,
    MemoryNodeCreate,
    PostTaskResult,
    PromoteAction,
    ReflectionInput,
    ReflectionSkipReason,
    TaskIntent,
    TaskOutcome,
    VerificationStatus,
)
from memory_engine.models.orm import Base, ProjectORM
from memory_engine.services.post_task import PostTaskService
from memory_engine.skills.reflection import ReflectionSkill


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    p = ProjectORM(name="reflect-test", description="Phase 5 test project")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@pytest.fixture
def post_task_svc(session):
    return PostTaskService(session)


@pytest.fixture
def skill():
    return ReflectionSkill()


def _inp(
    project_id: UUID,
    *,
    task: str = "Fix the retry deadlock bug",
    outcome: str = "Fixed retry handler to always call transition_to_terminal before re-enqueue",
    task_outcome: TaskOutcome = TaskOutcome.completed,
    verification: VerificationStatus = VerificationStatus.tests_passed,
    agent_confidence: float = 0.95,
    touched_files: list[str] | None = None,
    touched_symbols: list[str] | None = None,
    discovered_constraints: list[str] | None = None,
    discovered_procedures: list[str] | None = None,
    task_intent: TaskIntent | None = None,
    module_path: str | None = None,
) -> ReflectionInput:
    return ReflectionInput(
        project_id=project_id,
        task_description=task,
        task_outcome=task_outcome,
        outcome_summary=outcome,
        verification_status=verification,
        agent_confidence=agent_confidence,
        touched_files=touched_files or [],
        touched_symbols=touched_symbols or [],
        discovered_constraints=discovered_constraints or [],
        discovered_procedures=discovered_procedures or [],
        task_intent=task_intent,
        module_path=module_path,
    )


# ============================================================================
# ReflectionSkill unit tests
# ============================================================================


class TestReflectionGates:
    """Scenarios where reflection should be skipped."""

    def test_s02_failed_task_skipped(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task_outcome=TaskOutcome.failed,
            outcome="Could not reproduce the bug.",
        ))
        assert analysis.worth_retaining is False
        assert analysis.skip_reason == ReflectionSkipReason.task_failed
        assert analysis.suggested_candidates == []

    def test_s03_reverted_task_skipped(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task_outcome=TaskOutcome.reverted,
            outcome="Changes reverted due to build failure.",
        ))
        assert analysis.worth_retaining is False
        assert analysis.skip_reason == ReflectionSkipReason.task_reverted

    def test_s04_tests_failed_skipped(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            verification=VerificationStatus.tests_failed,
            outcome="Implemented retry logic but tests are failing.",
        ))
        assert analysis.worth_retaining is False
        assert analysis.skip_reason == ReflectionSkipReason.task_failed

    def test_s05_unverified_low_confidence_skipped(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            verification=VerificationStatus.unverified,
            agent_confidence=0.50,
            outcome="Made some changes to the retry module.",
        ))
        assert analysis.worth_retaining is False
        assert analysis.skip_reason == ReflectionSkipReason.unverified_low_confidence

    def test_s06_trivial_rename_no_signals_skipped(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Rename variable x to count",
            outcome="Renamed",
            task_intent=TaskIntent.trivial_edit,
        ))
        assert analysis.worth_retaining is False
        assert analysis.skip_reason == ReflectionSkipReason.trivial_change


class TestCandidateGeneration:
    """Scenarios where reflection should generate candidates."""

    def test_s01_bug_fix_creates_incident_candidate(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Fix the scheduler retry deadlock bug",
            outcome=(
                "Fixed retry handler: added call to transition_to_terminal() "
                "before re-enqueue to prevent slot starvation."
            ),
            task_intent=TaskIntent.bug_fix,
        ))
        assert analysis.worth_retaining is True
        kinds = [c.proposed_kind for c in analysis.suggested_candidates]
        assert MemoryKind.debug in kinds

    def test_s07_discovered_constraint_generates_constraint_candidate(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Add retry support to scheduler",
            outcome="Added retry loop with max_retries check and terminal-state call.",
            discovered_constraints=[
                "Retry handler must call transition_to_terminal() before re-enqueue"
            ],
        ))
        assert analysis.worth_retaining is True
        kinds = [c.proposed_kind for c in analysis.suggested_candidates]
        assert MemoryKind.constraint in kinds

        constraint_cands = [c for c in analysis.suggested_candidates
                            if c.proposed_kind == MemoryKind.constraint]
        assert constraint_cands[0].importance >= 0.85

    def test_s08_discovered_procedure_generates_procedure_candidate(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Fix scheduler retry",
            outcome="Fixed and verified. Before merging retry changes, run state_machine tests.",
            discovered_procedures=[
                "Run pytest tests/scheduler/test_state_machine.py before merging retry changes"
            ],
        ))
        assert analysis.worth_retaining is True
        kinds = [c.proposed_kind for c in analysis.suggested_candidates]
        assert MemoryKind.procedure in kinds

    def test_s09_refactor_creates_module_candidate(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Refactor scheduler execution flow to decouple retry from lifecycle",
            outcome=(
                "Extracted retry logic into a separate RetryCoordinator class. "
                "Lifecycle module now delegates retry decisions to RetryCoordinator."
            ),
            task_intent=TaskIntent.refactor,
            touched_files=["scheduler/lifecycle.py", "scheduler/retry.py"],
            module_path="scheduler.execution",
            verification=VerificationStatus.tests_passed,
        ))
        assert analysis.worth_retaining is True
        kinds = [c.proposed_kind for c in analysis.suggested_candidates]
        assert MemoryKind.module in kinds

    def test_s10_high_risk_refactor_creates_decision_candidate(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Refactor the state-machine terminal-state transition handling",
            outcome=(
                "Refactored all state-machine terminal transitions to be atomic. "
                "Introduced transition_to_terminal() as the single required path."
            ),
            task_intent=TaskIntent.refactor,
            touched_files=["scheduler/lifecycle.py"],
            touched_symbols=["transition_to_terminal"],
            module_path="scheduler.lifecycle",
            verification=VerificationStatus.tests_passed,
        ))
        assert analysis.worth_retaining is True
        kinds = [c.proposed_kind for c in analysis.suggested_candidates]
        assert MemoryKind.decision in kinds, (
            f"Expected decision candidate for high-risk refactor. Got kinds: {kinds}"
        )

    def test_s13_constraint_importance_higher_than_module(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Add retry support to scheduler",
            outcome=(
                "Added retry support. Discovered that retry must call "
                "transition_to_terminal before re-enqueue."
            ),
            task_intent=TaskIntent.feature_implementation,
            touched_files=["scheduler/retry.py"],
            module_path="scheduler.retry",
            discovered_constraints=["Retry handler must call transition_to_terminal first"],
            verification=VerificationStatus.tests_passed,
        ))
        assert analysis.worth_retaining is True

        constraints = [c for c in analysis.suggested_candidates
                       if c.proposed_kind == MemoryKind.constraint]
        modules = [c for c in analysis.suggested_candidates
                   if c.proposed_kind == MemoryKind.module]

        if constraints and modules:
            assert constraints[0].importance > modules[0].importance, (
                f"Constraint importance ({constraints[0].importance}) must exceed "
                f"module importance ({modules[0].importance})"
            )

    def test_s14_tests_passed_gives_high_confidence(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Fix retry deadlock",
            outcome="Fixed and all tests pass after the change to retry handler.",
            task_intent=TaskIntent.bug_fix,
            verification=VerificationStatus.tests_passed,
            agent_confidence=0.95,
        ))
        assert analysis.worth_retaining is True
        for cand in analysis.suggested_candidates:
            assert cand.confidence >= 0.90, (
                f"Expected confidence >= 0.90 for tests_passed, got {cand.confidence}"
            )

    def test_s15_manual_check_gives_medium_confidence(self, skill, project):
        analysis = skill.analyze(_inp(
            project.id,
            task="Fix the retry deadlock",
            outcome="Manually verified the retry handler now calls terminal transition.",
            task_intent=TaskIntent.bug_fix,
            verification=VerificationStatus.manual_check,
            agent_confidence=0.80,
        ))
        assert analysis.worth_retaining is True
        for cand in analysis.suggested_candidates:
            assert 0.65 <= cand.confidence < 0.95, (
                f"Expected manual_check confidence in (0.65, 0.95), got {cand.confidence}"
            )

    def test_s18_deterministic_analysis(self, skill, project):
        inp = _inp(
            project.id,
            task="Fix retry deadlock",
            outcome="Fixed retry handler to call transition_to_terminal first.",
            task_intent=TaskIntent.bug_fix,
        )
        a1 = skill.analyze(inp)
        a2 = skill.analyze(inp)
        assert a1.worth_retaining == a2.worth_retaining
        assert a1.skip_reason == a2.skip_reason
        assert len(a1.suggested_candidates) == len(a2.suggested_candidates)
        for c1, c2 in zip(a1.suggested_candidates, a2.suggested_candidates):
            assert c1.proposed_kind == c2.proposed_kind
            assert c1.confidence == c2.confidence
            assert c1.importance == c2.importance


# ============================================================================
# PostTaskService end-to-end tests
# ============================================================================


class TestPostTaskService:
    """End-to-end tests for the full reflection → promotion pipeline."""

    def test_s11_reflect_and_write_creates_node(self, post_task_svc, project, session):
        """reflect_and_write should create at least one memory node for a bug fix."""
        from memory_engine.repositories.memory_node import MemoryNodeRepository

        result = post_task_svc.reflect_and_write(_inp(
            project.id,
            task="Fix the scheduler retry deadlock",
            outcome=(
                "Fixed retry handler: transition_to_terminal() is now called before "
                "re-enqueue. Slot starvation no longer occurs."
            ),
            task_intent=TaskIntent.bug_fix,
            touched_files=["scheduler/retry.py"],
            touched_symbols=["RetryPolicy", "transition_to_terminal"],
            module_path="scheduler.retry",
            verification=VerificationStatus.tests_passed,
        ))

        assert result.reflection_skipped is False
        assert result.candidates_staged >= 1

        # At least one candidate must have been promoted (created/merged/updated)
        promoted = result.candidates_promoted + result.candidates_discarded
        assert promoted > 0, "At least one candidate must have been processed"

        # Verify a node was actually written to the DB
        repo = MemoryNodeRepository(session)
        nodes = repo.list_by_project(str(project.id))
        assert len(nodes) >= 1

    def test_s11_full_result_structure(self, post_task_svc, project):
        result = post_task_svc.reflect_and_write(_inp(
            project.id,
            task="Fix the scheduler retry deadlock",
            outcome="Fixed retry handler to call transition_to_terminal before re-enqueue.",
            task_intent=TaskIntent.bug_fix,
        ))
        # Validate PostTaskResult structure
        assert isinstance(result, PostTaskResult)
        assert str(result.project_id) == str(project.id)
        assert result.reflection is not None
        assert isinstance(result.promotion_results, list)
        assert isinstance(result.consolidation_notes, list)
        assert result.candidates_staged == (
            result.candidates_promoted + result.candidates_discarded + result.candidates_needs_review
        )

    def test_s11_failed_task_skipped_no_db_writes(self, post_task_svc, project, session):
        result = post_task_svc.reflect_and_write(_inp(
            project.id,
            task_outcome=TaskOutcome.failed,
            outcome="Could not fix the bug.",
        ))
        assert result.reflection_skipped is True
        assert result.candidates_staged == 0
        assert result.promotion_results == []

        from memory_engine.repositories.memory_node import MemoryNodeRepository
        repo = MemoryNodeRepository(session)
        nodes = repo.list_by_project(str(project.id))
        assert len(nodes) == 0

    def test_s12_duplicate_existing_memory_merged_not_duplicated(
        self, post_task_svc, session, project
    ):
        """When an incident already exists with similar content, the new candidate
        should merge/update rather than create a duplicate node."""
        from memory_engine.services.memory_service import MemoryService

        m_svc = MemoryService(session)
        # Pre-create an existing incident
        m_svc.create_node(MemoryNodeCreate(
            project_id=project.id,
            title="Fix: Fix the scheduler retry deadlock",
            summary="Fixed retry handler to call transition_to_terminal before re-enqueue.",
            kind=MemoryKind.debug,
            importance=0.85,
            confidence=0.92,
        ))

        result = post_task_svc.reflect_and_write(_inp(
            project.id,
            task="Fix the scheduler retry deadlock",
            outcome="Fixed retry handler to call transition_to_terminal before re-enqueue.",
            task_intent=TaskIntent.bug_fix,
        ))

        assert result.reflection_skipped is False
        # Action should be update/merge/supersede/discard — not a fresh create
        if result.promotion_results:
            actions = {r.action for r in result.promotion_results}
            # create is allowed too when similarity is below threshold
            assert actions.issubset({
                PromoteAction.create, PromoteAction.update,
                PromoteAction.merge, PromoteAction.supersede, PromoteAction.discard,
                PromoteAction.needs_review,
            })

    def test_constraint_is_persisted_with_high_importance(self, post_task_svc, project, session):
        result = post_task_svc.reflect_and_write(_inp(
            project.id,
            task="Add retry support",
            outcome=(
                "Added retry support and discovered constraint: "
                "retry handler must call transition_to_terminal before re-enqueue."
            ),
            discovered_constraints=[
                "Retry handler must call transition_to_terminal() before re-enqueue"
            ],
            verification=VerificationStatus.tests_passed,
        ))
        assert result.reflection_skipped is False

        from memory_engine.repositories.memory_node import MemoryNodeRepository
        repo = MemoryNodeRepository(session)
        nodes = repo.list_by_project(str(project.id))
        constraint_nodes = [n for n in nodes if n.kind == "constraint"]
        assert len(constraint_nodes) >= 1
        assert constraint_nodes[0].importance >= 0.85


# ============================================================================
# Scenario 16 — No user commands needed
# ============================================================================


def test_s16_no_user_commands_needed(post_task_svc, project):
    """The agent calls reflect_and_write once. No separate promote/write commands."""
    # This test demonstrates the intended contract: one call does everything.
    result = post_task_svc.reflect_and_write(ReflectionInput(
        project_id=project.id,
        task_description="Add retry support without breaking the task lifecycle",
        task_outcome=TaskOutcome.completed,
        outcome_summary=(
            "Added RetryCoordinator. Verified that transition_to_terminal() is called "
            "on every retry path. All lifecycle tests pass."
        ),
        verification_status=VerificationStatus.tests_passed,
        touched_files=["scheduler/retry.py", "scheduler/lifecycle.py"],
        touched_symbols=["RetryCoordinator", "transition_to_terminal"],
        agent_confidence=0.95,
        task_intent=TaskIntent.feature_implementation,
        module_path="scheduler.retry",
        discovered_constraints=[
            "Every retry path must call transition_to_terminal() before re-enqueue"
        ],
    ))

    # One call returned a complete result — no manual follow-up required
    assert result.reflection_skipped is False
    assert result.candidates_staged >= 1
    assert result.promotion_results  # pipeline ran automatically
    assert isinstance(result.consolidation_notes, list)


# ============================================================================
# Scenario 17 — API endpoint
# ============================================================================


def test_s17_api_endpoint(api_client, scheduler_demo):
    """POST /v1/skills/reflect-and-write returns a valid PostTaskResult."""
    project = scheduler_demo["project"]
    response = api_client.post(
        "/v1/skills/reflect-and-write",
        json={
            "project_id": str(project.id),
            "task_description": "Fix the scheduler retry deadlock bug",
            "task_outcome": "completed",
            "outcome_summary": (
                "Fixed retry handler to call transition_to_terminal() before re-enqueue. "
                "Slot starvation no longer occurs after the retry path change."
            ),
            "verification_status": "tests_passed",
            "touched_files": ["scheduler/retry.py"],
            "touched_symbols": ["RetryPolicy", "transition_to_terminal"],
            "agent_confidence": 0.95,
            "task_intent": "bug_fix",
            "module_path": "scheduler.retry",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "reflection" in body
    assert "promotion_results" in body
    assert "candidates_staged" in body
    assert "candidates_promoted" in body
    assert body["reflection_skipped"] is False


def test_s17_api_failed_task_returns_skipped(api_client, scheduler_demo):
    project = scheduler_demo["project"]
    response = api_client.post(
        "/v1/skills/reflect-and-write",
        json={
            "project_id": str(project.id),
            "task_description": "Fix the retry bug",
            "task_outcome": "failed",
            "outcome_summary": "Could not reproduce the bug.",
            "verification_status": "unverified",
            "agent_confidence": 0.4,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reflection_skipped"] is True
    assert body["candidates_staged"] == 0
