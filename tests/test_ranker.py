"""Tests for DeterministicRanker — scoring and ranking correctness."""

from datetime import datetime, timezone

import pytest

from memory_engine.models.domain import (
    MemoryKind,
    MemoryNode,
    MemoryStatus,
    TaskIntent,
)
from memory_engine.skills.ranker import DeterministicRanker
from uuid import uuid4


def _node(
    title: str,
    summary: str,
    kind: MemoryKind = MemoryKind.module,
    tags: list[str] | None = None,
    importance: float = 0.5,
    confidence: float = 1.0,
    depth: int = 0,
    module_path: str | None = None,
    status: MemoryStatus = MemoryStatus.active,
) -> MemoryNode:
    now = datetime.now(timezone.utc)
    return MemoryNode(
        id=uuid4(),
        project_id=uuid4(),
        title=title,
        summary=summary,
        kind=kind,
        tags=tags or [],
        importance=importance,
        confidence=confidence,
        depth=depth,
        module_path=module_path,
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture()
def ranker() -> DeterministicRanker:
    return DeterministicRanker()


def test_score_breakdown_keys(ranker):
    node = _node("Auth module", "Handles JWT tokens", kind=MemoryKind.module)
    sm = ranker.score(node, task="fix auth bug", intent=TaskIntent.bug_fix,
                      current_files=[], current_symbols=[])
    keys = set(sm.score_breakdown.keys())
    assert "lexical_similarity" in keys
    assert "module_path_overlap" in keys
    assert "symbol_overlap" in keys
    assert "task_intent_compatibility" in keys
    assert "tree_proximity" in keys
    assert "importance" in keys
    assert "confidence" in keys
    assert "freshness" in keys
    assert "final_score" in keys


def test_score_in_range(ranker):
    node = _node("Scheduler module", "Manages task execution")
    sm = ranker.score(node, task="fix scheduler bug", intent=TaskIntent.bug_fix,
                      current_files=[], current_symbols=[])
    assert 0.0 <= sm.score <= 1.0


def test_higher_lexical_similarity_raises_score(ranker):
    """A node whose title/summary share words with the task should score higher."""
    relevant = _node("Retry logic", "retry backoff scheduler tasks", kind=MemoryKind.debug)
    irrelevant = _node("UI theme", "color palettes and fonts")

    sm_rel = ranker.score(relevant, task="fix retry backoff scheduler",
                          intent=TaskIntent.bug_fix, current_files=[], current_symbols=[])
    sm_irr = ranker.score(irrelevant, task="fix retry backoff scheduler",
                          intent=TaskIntent.bug_fix, current_files=[], current_symbols=[])
    assert sm_rel.score > sm_irr.score


def test_module_path_overlap_improves_ranking(ranker):
    """Node with matching module_path should score higher for same task."""
    matched = _node("Retry Policy", "retry logic", module_path="scheduler.retry")
    unmatched = _node("Retry Policy", "retry logic", module_path="payment.billing")

    sm_match = ranker.score(matched, task="fix retry bug",
                            intent=TaskIntent.bug_fix,
                            current_files=["scheduler/retry.py"],
                            current_symbols=[])
    sm_no = ranker.score(unmatched, task="fix retry bug",
                         intent=TaskIntent.bug_fix,
                         current_files=["scheduler/retry.py"],
                         current_symbols=[])
    assert sm_match.score_breakdown["module_path_overlap"] > sm_no.score_breakdown["module_path_overlap"]
    assert sm_match.score > sm_no.score


def test_incident_memories_prioritised_for_bug_fix(ranker):
    """Debug/incident nodes should rank above generic modules for bug-fix intent."""
    incident = _node("Missing terminal state causes deadlock", "incident",
                     kind=MemoryKind.debug, importance=0.9)
    module = _node("UI component library", "buttons and inputs",
                   kind=MemoryKind.module, importance=0.5)

    sm_inc = ranker.score(incident, task="fix scheduler retry deadlock",
                          intent=TaskIntent.bug_fix, current_files=[], current_symbols=[])
    sm_mod = ranker.score(module, task="fix scheduler retry deadlock",
                          intent=TaskIntent.bug_fix, current_files=[], current_symbols=[])
    # incident has higher intent-compat for bug_fix
    assert sm_inc.score_breakdown["task_intent_compatibility"] > sm_mod.score_breakdown["task_intent_compatibility"]


def test_symbol_overlap_raises_score(ranker):
    node = _node("RetryPolicy class", "implements retry logic",
                 tags=["RetryPolicy", "backoff"])
    sm = ranker.score(node, task="fix retry",
                      intent=TaskIntent.bug_fix,
                      current_files=[],
                      current_symbols=["RetryPolicy"])
    assert sm.score_breakdown["symbol_overlap"] > 0.0


def test_depth_zero_higher_proximity_than_depth_3(ranker):
    shallow = _node("Root arch", "overview", depth=0)
    deep = _node("Nested detail", "deep", depth=3)
    sm_s = ranker.score(shallow, task="explain", intent=TaskIntent.code_explanation,
                        current_files=[], current_symbols=[])
    sm_d = ranker.score(deep, task="explain", intent=TaskIntent.code_explanation,
                        current_files=[], current_symbols=[])
    assert sm_s.score_breakdown["tree_proximity"] > sm_d.score_breakdown["tree_proximity"]


def test_rank_returns_sorted_descending(ranker):
    nodes = [
        _node("Unrelated thing", "css styling"),
        _node("Scheduler retry", "retry scheduler backoff", kind=MemoryKind.debug, importance=0.9),
        _node("Auth module", "jwt tokens"),
    ]
    scored = ranker.rank(nodes, task="fix scheduler retry bug",
                         intent=TaskIntent.bug_fix, current_files=[], current_symbols=[])
    scores = [s.score for s in scored]
    assert scores == sorted(scores, reverse=True)


def test_same_request_same_score(ranker):
    """Score must be deterministic."""
    node = _node("Retry logic", "retry backoff scheduler", kind=MemoryKind.debug)
    sm1 = ranker.score(node, task="fix retry deadlock", intent=TaskIntent.bug_fix,
                       current_files=[], current_symbols=[])
    sm2 = ranker.score(node, task="fix retry deadlock", intent=TaskIntent.bug_fix,
                       current_files=[], current_symbols=[])
    assert sm1.score == sm2.score
    assert sm1.score_breakdown == sm2.score_breakdown
