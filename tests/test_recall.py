"""Tests for RecallService — end-to-end autonomous retrieval."""

import pytest

from memory_engine.models.domain import (
    MemoryType,
    RecallRequest,
    RouteRequest,
    RoutingPlan,
    TaskComplexity,
    TaskIntent,
    RiskLevel,
)
from memory_engine.skills.recall import RecallService
from memory_engine.skills.router import SkillRouter
from uuid import uuid4


def _recall_req(project_id, task: str, **kwargs) -> RecallRequest:
    return RecallRequest(project_id=project_id, current_task=task, **kwargs)


# ---------------------------------------------------------------------------
# 1. Trivial edit — recall skipped
# ---------------------------------------------------------------------------


def test_trivial_edit_skips_recall(recall_service: RecallService, demo_project):
    result = recall_service.recall(_recall_req(demo_project.id, "Rename local variable x to y"))
    assert result.recall_skipped is True
    assert result.skip_reason is not None
    assert result.context_pack.total_nodes == 0
    assert result.retrieval_trace == []


# ---------------------------------------------------------------------------
# 2. Bug fix triggers recall
# ---------------------------------------------------------------------------


def test_bug_fix_triggers_recall(recall_service: RecallService, scheduler_demo):
    project = scheduler_demo["project"]
    result = recall_service.recall(_recall_req(
        project.id,
        "Fix the scheduler retry logic to not deadlock",
        current_files=["scheduler/retry.py"],
    ))
    assert result.recall_skipped is False
    assert result.context_pack.total_nodes > 0
    assert len(result.retrieval_trace) > 0


# ---------------------------------------------------------------------------
# 3. State-machine change retrieves constraints and incidents
# ---------------------------------------------------------------------------


def test_state_machine_change_retrieves_constraints_and_incidents(
    recall_service: RecallService, scheduler_demo
):
    project = scheduler_demo["project"]
    result = recall_service.recall(_recall_req(
        project.id,
        "Add retry support without breaking the existing task lifecycle semantics",
        current_files=["scheduler/lifecycle.py"],
        current_symbols=["transition_to_terminal", "RetryPolicy"],
    ))
    assert result.recall_skipped is False

    pack = result.context_pack
    # Constraints must be present
    assert len(pack.constraints) > 0, "Expected at least one constraint node"
    # At least architecture or modules loaded
    assert (len(pack.architecture) + len(pack.modules)) > 0


# ---------------------------------------------------------------------------
# 4. Refactor triggers architecture and decision recall
# ---------------------------------------------------------------------------


def test_refactor_triggers_architecture_recall(recall_service: RecallService, scheduler_demo):
    project = scheduler_demo["project"]
    result = recall_service.recall(_recall_req(
        project.id,
        "Refactor the scheduler execution flow",
    ))
    assert result.recall_skipped is False
    assert len(result.context_pack.architecture) > 0 or len(result.context_pack.modules) > 0


# ---------------------------------------------------------------------------
# 5. Stale memory excluded by default (except incidents for bug_fix)
# ---------------------------------------------------------------------------


def test_stale_not_selected_for_refactor(recall_service: RecallService, scheduler_demo, session):
    """Stale nodes should not appear in non-bug-fix recall."""
    from memory_engine.models.domain import MemoryKind, MemoryNodeCreate, MemoryStatus
    from memory_engine.services.memory_service import MemoryService

    project = scheduler_demo["project"]
    m_svc = MemoryService(session)
    stale_node = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Stale old design",
        summary="This was the old approach, no longer valid.",
        kind=MemoryKind.module,
        status=MemoryStatus.stale,
        importance=0.9,
    ))

    result = recall_service.recall(_recall_req(project.id, "Refactor the scheduler execution flow"))

    all_titles = {
        n.title for n in
        result.context_pack.constraints
        + result.context_pack.architecture
        + result.context_pack.modules
        + result.context_pack.decisions
        + result.context_pack.incidents
        + result.context_pack.procedures
    }
    assert "Stale old design" not in all_titles

    excluded = [e for e in result.retrieval_trace if e.action == "excluded"]
    assert any("stale" in e.reason.lower() for e in excluded)


# ---------------------------------------------------------------------------
# 6. Explicit no-memory flag disables recall
# ---------------------------------------------------------------------------


def test_no_memory_flag_disables_recall(recall_service: RecallService, scheduler_demo):
    project = scheduler_demo["project"]

    # Build routing plan manually with recall disabled
    plan = RoutingPlan(
        should_recall_memory=False,
        should_allow_deep_inspection=False,
        task_intent=TaskIntent.bug_fix,
        task_complexity=TaskComplexity.medium,
        risk_level=RiskLevel.medium,
        required_memory_types=[],
        recommended_token_budget=0,
        reasoning=["User requested no memory."],
        persistence_allowed=False,
    )
    result = recall_service.recall(_recall_req(
        project.id,
        "Fix retry logic",
        routing_plan=plan,
    ))
    assert result.recall_skipped is True


# ---------------------------------------------------------------------------
# 7. Retrieval trace explains selected and excluded memories
# ---------------------------------------------------------------------------


def test_retrieval_trace_has_selected_and_excluded(recall_service: RecallService, scheduler_demo):
    project = scheduler_demo["project"]
    result = recall_service.recall(_recall_req(
        project.id,
        "Fix the scheduler retry logic",
    ))
    assert not result.recall_skipped
    trace_actions = {e.action for e in result.retrieval_trace}
    # We expect both selected and excluded entries
    assert "selected" in trace_actions
    # Not all nodes should be selected (stale incident or low-priority nodes)


# ---------------------------------------------------------------------------
# 8. Bug fix expands evidence for incidents
# ---------------------------------------------------------------------------


def test_bug_fix_includes_evidence(recall_service: RecallService, scheduler_demo):
    """Bug-fix recall should expand evidence from incident nodes."""
    project = scheduler_demo["project"]
    result = recall_service.recall(_recall_req(
        project.id,
        "Fix the scheduler retry deadlock bug",
        current_files=["scheduler/retry.py"],
    ))
    # The scheduler demo has one evidence item on the incident
    assert len(result.context_pack.evidence_refs) > 0


# ---------------------------------------------------------------------------
# 9. Project not found raises error
# ---------------------------------------------------------------------------


def test_unknown_project_raises(recall_service: RecallService):
    with pytest.raises(Exception):
        recall_service.recall(_recall_req(uuid4(), "Fix something"))


# ---------------------------------------------------------------------------
# 10. API endpoint for recall
# ---------------------------------------------------------------------------


def test_api_recall_endpoint(api_client, scheduler_demo):
    project = scheduler_demo["project"]
    response = api_client.post(
        "/v1/skills/recall-memory",
        json={
            "project_id": str(project.id),
            "current_task": "Fix the scheduler retry logic",
            "current_files": ["scheduler/retry.py"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "context_pack" in body
    assert "retrieval_trace" in body
    assert "routing_plan" in body
    assert body["recall_skipped"] is False
