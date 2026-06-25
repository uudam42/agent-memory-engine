"""Tests for the SkillRouter — all assertions on deterministic output."""

import pytest

from memory_engine.models.domain import (
    MemoryType,
    RiskLevel,
    RouteRequest,
    TaskComplexity,
    TaskIntent,
)
from memory_engine.skills.router import SkillRouter
from uuid import uuid4


@pytest.fixture()
def router() -> SkillRouter:
    return SkillRouter()


def _req(task: str, **kwargs) -> RouteRequest:
    return RouteRequest(project_id=uuid4(), current_task=task, **kwargs)


# ---------------------------------------------------------------------------
# 1. Trivial edit skips recall by default
# ---------------------------------------------------------------------------


def test_trivial_rename_skips_recall(router):
    plan = router.route(_req("Rename the local variable x to count"))
    assert plan.task_intent == TaskIntent.trivial_edit
    assert plan.should_recall_memory is False
    assert plan.task_complexity == TaskComplexity.trivial
    assert plan.recommended_token_budget == 0


def test_trivial_formatting_skips_recall(router):
    plan = router.route(_req("Fix whitespace and lint errors in utils.py"))
    assert plan.should_recall_memory is False


def test_trivial_with_state_machine_elevates_risk(router):
    """Trivial edit that touches state machine code should elevate to recall."""
    plan = router.route(_req("Fix whitespace in state_machine.py", current_files=["state_machine.py"]))
    # Risk is elevated by the high-risk filename pattern
    assert plan.risk_level in (RiskLevel.medium, RiskLevel.high)
    assert plan.should_recall_memory is True


# ---------------------------------------------------------------------------
# 2. Bug fix triggers recall
# ---------------------------------------------------------------------------


def test_bug_fix_triggers_recall(router):
    plan = router.route(_req("Fix the scheduler retry logic"))
    assert plan.task_intent == TaskIntent.bug_fix
    assert plan.should_recall_memory is True
    assert plan.risk_level in (RiskLevel.medium, RiskLevel.high)


def test_bug_fix_includes_incident_and_decision_memory(router):
    plan = router.route(_req("Fix the scheduler retry logic"))
    assert MemoryType.incident_memory in plan.required_memory_types
    assert MemoryType.decision_memory in plan.required_memory_types
    assert MemoryType.constraint_memory in plan.required_memory_types


# ---------------------------------------------------------------------------
# 3. State-machine change triggers recall with constraints and incidents
# ---------------------------------------------------------------------------


def test_state_machine_change_high_risk(router):
    plan = router.route(_req(
        "Add retry support without breaking the existing task lifecycle semantics",
        current_files=["scheduler/lifecycle.py"],
        current_symbols=["transition_to_terminal", "RetryPolicy"],
    ))
    assert plan.should_recall_memory is True
    assert plan.risk_level == RiskLevel.high
    assert MemoryType.constraint_memory in plan.required_memory_types
    assert MemoryType.incident_memory in plan.required_memory_types
    assert plan.should_allow_deep_inspection is True


def test_state_machine_keyword_in_task(router):
    plan = router.route(_req("Refactor the state-machine to support explicit cancellation"))
    assert plan.risk_level == RiskLevel.high
    assert MemoryType.constraint_memory in plan.required_memory_types


# ---------------------------------------------------------------------------
# 4. Refactor triggers architecture and decision recall
# ---------------------------------------------------------------------------


def test_refactor_triggers_architecture_recall(router):
    plan = router.route(_req("Refactor the scheduler execution flow"))
    assert plan.task_intent == TaskIntent.refactor
    assert plan.should_recall_memory is True
    assert MemoryType.semantic_memory in plan.required_memory_types
    assert MemoryType.decision_memory in plan.required_memory_types


# ---------------------------------------------------------------------------
# 5 & 6. Stale / superseded filtering (router only checks flags)
# ---------------------------------------------------------------------------


def test_no_memory_flag_disables_recall(router):
    """Explicit user instruction to avoid memory disables recall."""
    plan = router.route(_req(
        "Fix the scheduler retry logic",
        user_instruction_flags=["no_memory"],
    ))
    assert plan.should_recall_memory is False
    assert plan.persistence_allowed is False


def test_isolated_flag_disables_recall(router):
    plan = router.route(_req(
        "Implement feature X",
        user_instruction_flags=["isolated"],
    ))
    assert plan.should_recall_memory is False


# ---------------------------------------------------------------------------
# 7. Router produces deterministic output for same request
# ---------------------------------------------------------------------------


def test_deterministic_output(router):
    req = _req(
        "Fix the scheduler retry logic",
        current_files=["scheduler/retry.py"],
        current_symbols=["RetryPolicy"],
    )
    plan_a = router.route(req)
    plan_b = router.route(req)
    assert plan_a.model_dump() == plan_b.model_dump()


# ---------------------------------------------------------------------------
# 8. risk_hint only elevates, never lowers
# ---------------------------------------------------------------------------


def test_risk_hint_elevates(router):
    plan = router.route(_req("Rename a variable", risk_hint=RiskLevel.high))
    assert plan.risk_level == RiskLevel.high


def test_risk_hint_cannot_lower(router):
    """A trivial_edit that passes risk_hint=low should not lower bug-fix risk."""
    plan = router.route(_req(
        "Fix the authentication token expiry bug",
        risk_hint=RiskLevel.low,
    ))
    # auth is a high-risk keyword — risk must stay elevated
    assert plan.risk_level in (RiskLevel.medium, RiskLevel.high)


# ---------------------------------------------------------------------------
# 9. Feature implementation triggers correct memory types
# ---------------------------------------------------------------------------


def test_feature_implementation_memory_types(router):
    plan = router.route(_req("Add retry support to the task scheduler"))
    assert plan.task_intent == TaskIntent.feature_implementation
    assert MemoryType.semantic_memory in plan.required_memory_types
    assert MemoryType.constraint_memory in plan.required_memory_types
    assert plan.should_recall_memory is True


# ---------------------------------------------------------------------------
# 10. Repository onboarding triggers full recall
# ---------------------------------------------------------------------------


def test_onboarding_triggers_broad_recall(router):
    plan = router.route(_req("Onboarding: explain how the scheduler subsystem works"))
    assert plan.should_recall_memory is True
    assert MemoryType.semantic_memory in plan.required_memory_types
    assert MemoryType.procedural_memory in plan.required_memory_types


# ---------------------------------------------------------------------------
# 11. Reasoning is always populated when recall is triggered
# ---------------------------------------------------------------------------


def test_routing_plan_has_reasoning(router):
    plan = router.route(_req("Fix the bug in the scheduler"))
    assert len(plan.reasoning) > 0
    assert any("intent=" in r for r in plan.reasoning)
