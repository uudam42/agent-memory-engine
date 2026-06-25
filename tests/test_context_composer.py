"""Tests for ContextComposer — budget enforcement and priority ordering."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from memory_engine.models.domain import (
    EnrichedContextPack,
    MemoryKind,
    MemoryNode,
    MemoryStatus,
    MemoryType,
    Project,
    RiskLevel,
    RoutingPlan,
    ScoredMemory,
    TaskComplexity,
    TaskIntent,
)
from memory_engine.skills.composer import ContextComposer


def _now():
    return datetime.now(timezone.utc)


def _project() -> Project:
    return Project(
        id=uuid4(),
        name="test-project",
        created_at=_now(),
        updated_at=_now(),
    )


def _plan(intent: TaskIntent = TaskIntent.bug_fix, budget: int = 6000) -> RoutingPlan:
    return RoutingPlan(
        should_recall_memory=True,
        should_allow_deep_inspection=False,
        task_intent=intent,
        task_complexity=TaskComplexity.medium,
        risk_level=RiskLevel.medium,
        required_memory_types=[MemoryType.semantic_memory],
        recommended_token_budget=budget,
        reasoning=["test"],
        persistence_allowed=True,
    )


def _node(
    title: str,
    kind: MemoryKind,
    importance: float = 0.5,
    confidence: float = 1.0,
    status: MemoryStatus = MemoryStatus.active,
    summary: str = "summary",
) -> MemoryNode:
    return MemoryNode(
        id=uuid4(),
        project_id=uuid4(),
        title=title,
        summary=summary,
        kind=kind,
        tags=[],
        importance=importance,
        confidence=confidence,
        status=status,
        depth=0,
        created_at=_now(),
        updated_at=_now(),
    )


def _sm(node: MemoryNode, score: float = 0.8) -> ScoredMemory:
    return ScoredMemory(node=node, score=score, score_breakdown={"final_score": score})


@pytest.fixture()
def composer() -> ContextComposer:
    return ContextComposer()


# ---------------------------------------------------------------------------
# 1. Constraints are always retained under token pressure
# ---------------------------------------------------------------------------


def test_constraints_retained_under_pressure(composer):
    """Constraint nodes always fill before other kinds."""
    project = _project()
    plan = _plan(budget=200)  # very tight budget

    c1 = _node("Constraint A", MemoryKind.constraint, importance=1.0)
    c2 = _node("Constraint B", MemoryKind.constraint, importance=1.0)
    m1 = _node("Module X", MemoryKind.module, importance=0.5)

    scored = [_sm(c1, 0.9), _sm(c2, 0.9), _sm(m1, 0.8)]
    pack, trace = composer.compose(
        project=project,
        scored_nodes=scored,
        routing_plan=plan,
    )
    constraint_titles = {n.title for n in pack.constraints}
    assert "Constraint A" in constraint_titles or "Constraint B" in constraint_titles
    # Under tight budget, at least one constraint fits before modules
    module_titles = {n.title for n in pack.modules}
    # Constraints have priority — even if modules are excluded, constraints are first
    assert len(pack.constraints) >= 1 or len(pack.modules) == 0


# ---------------------------------------------------------------------------
# 2. Stale memory is excluded by default
# ---------------------------------------------------------------------------


def test_stale_memory_excluded(composer):
    project = _project()
    plan = _plan()

    stale = _node("Old design", MemoryKind.module, status=MemoryStatus.stale)
    active = _node("Current design", MemoryKind.module, status=MemoryStatus.active)

    scored = [_sm(stale, 0.9), _sm(active, 0.8)]
    pack, trace = composer.compose(
        project=project, scored_nodes=scored, routing_plan=plan
    )

    selected_titles = {n.title for n in pack.modules}
    assert "Current design" in selected_titles
    assert "Old design" not in selected_titles

    excluded_trace = [e for e in trace if e.action == "excluded"]
    assert any("stale" in e.reason.lower() for e in excluded_trace)


# ---------------------------------------------------------------------------
# 3. Superseded memory is excluded by default
# ---------------------------------------------------------------------------


def test_superseded_memory_excluded(composer):
    project = _project()
    plan = _plan()

    superseded = _node("Legacy design", MemoryKind.decision, status=MemoryStatus.superseded)
    active_decision = _node("Current decision", MemoryKind.decision, status=MemoryStatus.active)

    scored = [_sm(superseded, 0.95), _sm(active_decision, 0.7)]
    pack, trace = composer.compose(
        project=project, scored_nodes=scored, routing_plan=plan
    )

    dec_titles = {n.title for n in pack.decisions}
    assert "Legacy design" not in dec_titles
    assert "Current decision" in dec_titles


# ---------------------------------------------------------------------------
# 4. Budget trimming removes lower-importance memory first
# ---------------------------------------------------------------------------


def test_budget_trimming_removes_lower_importance(composer):
    project = _project()
    plan = _plan(budget=300)  # tight budget

    high_imp = _node("Critical procedure", MemoryKind.procedure, importance=0.9)
    low_imp = _node("Optional procedure", MemoryKind.procedure, importance=0.1)

    scored = [_sm(high_imp, 0.8), _sm(low_imp, 0.7)]
    pack, trace = composer.compose(
        project=project, scored_nodes=scored, routing_plan=plan
    )

    proc_titles = {n.title for n in pack.procedures}
    # low_imp might be excluded by the importance gate (0.3 threshold)
    excluded = [e for e in trace if e.action == "excluded" and "importance" in e.reason.lower()]
    assert len(excluded) > 0 or "Optional procedure" not in proc_titles


# ---------------------------------------------------------------------------
# 5. Retrieval trace explains selected and excluded memories
# ---------------------------------------------------------------------------


def test_retrieval_trace_coverage(composer):
    project = _project()
    plan = _plan()

    included = _node("Good module", MemoryKind.module, status=MemoryStatus.active)
    excluded_stale = _node("Old module", MemoryKind.module, status=MemoryStatus.stale)

    scored = [_sm(included, 0.9), _sm(excluded_stale, 0.8)]
    pack, trace = composer.compose(
        project=project, scored_nodes=scored, routing_plan=plan
    )

    actions = {e.action for e in trace}
    assert "selected" in actions
    assert "excluded" in actions

    # Every trace entry has a non-empty reason
    for entry in trace:
        assert entry.reason


# ---------------------------------------------------------------------------
# 6. Typed buckets are populated correctly
# ---------------------------------------------------------------------------


def test_typed_buckets(composer):
    project = _project()
    plan = _plan(intent=TaskIntent.refactor)

    nodes_by_kind = {
        MemoryKind.constraint: _node("C", MemoryKind.constraint),
        MemoryKind.architecture: _node("A", MemoryKind.architecture),
        MemoryKind.module: _node("M", MemoryKind.module),
        MemoryKind.decision: _node("D", MemoryKind.decision),
        MemoryKind.debug: _node("I", MemoryKind.debug),
        MemoryKind.procedure: _node("P", MemoryKind.procedure),
    }
    scored = [_sm(n, 0.8) for n in nodes_by_kind.values()]
    pack, _ = composer.compose(project=project, scored_nodes=scored, routing_plan=plan)

    assert len(pack.constraints) == 1
    assert len(pack.architecture) == 1
    assert len(pack.modules) == 1
    assert len(pack.decisions) == 1
    assert len(pack.incidents) == 1
    assert len(pack.procedures) == 1


# ---------------------------------------------------------------------------
# 7. as_text() produces readable output
# ---------------------------------------------------------------------------


def test_as_text_output(composer):
    project = _project()
    plan = _plan()

    c = _node("Must not break state machine", MemoryKind.constraint, summary="Critical constraint")
    scored = [_sm(c, 0.9)]
    pack, _ = composer.compose(project=project, scored_nodes=scored, routing_plan=plan)

    text = pack.as_text()
    assert "Constraints" in text
    assert "Must not break state machine" in text
    assert "token estimate" in text
