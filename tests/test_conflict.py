"""Tests for ConflictService — Phase 3."""
import pytest

from memory_engine.models.domain import ConflictKind, DuplicateMatch, MemoryKind
from memory_engine.services.conflict import ConflictService
from tests.helpers import make_candidate, make_node

svc = ConflictService()


def _match(node, candidate) -> DuplicateMatch:
    return DuplicateMatch(
        existing_node=node,
        similarity_score=0.95,
        title_similarity=0.95,
        module_overlap=0.0,
        is_same_kind=True,
    )


class TestConfidenceTooLow:
    def test_candidate_confidence_much_lower_blocked(self):
        existing = make_node(title="Auth Arch", kind="architecture", confidence=0.95)
        cand = make_candidate(title="Auth Arch", kind=MemoryKind.architecture, confidence=0.70)
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is not None
        assert result.kind == ConflictKind.confidence_too_low

    def test_candidate_within_tolerance_allowed(self):
        existing = make_node(title="Auth Arch", kind="architecture", confidence=0.90)
        cand = make_candidate(title="Auth Arch", kind=MemoryKind.architecture, confidence=0.80)
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is None or result.kind != ConflictKind.confidence_too_low

    def test_equal_confidence_no_conflict(self):
        existing = make_node(title="Auth Arch", kind="architecture", confidence=0.85)
        cand = make_candidate(title="Auth Arch", kind=MemoryKind.architecture, confidence=0.85)
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is None

    def test_higher_candidate_confidence_no_conflict(self):
        existing = make_node(title="Auth Arch", kind="architecture", confidence=0.70)
        cand = make_candidate(title="Auth Arch", kind=MemoryKind.architecture, confidence=0.95)
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is None


class TestContentContradiction:
    def test_must_vs_optional_is_contradiction(self):
        existing = make_node(title="API Auth", kind="architecture",
                             summary="Authentication is required and must always be used.")
        cand = make_candidate(title="API Auth", kind=MemoryKind.architecture,
                              confidence=0.80,
                              summary="Authentication is optional and may be skipped.")
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is not None
        assert result.kind == ConflictKind.content_contradiction

    def test_synchronous_vs_async_is_contradiction(self):
        existing = make_node(title="Job Processing", kind="architecture",
                             summary="Jobs are processed synchronously and blocking.")
        cand = make_candidate(title="Job Processing", kind=MemoryKind.architecture,
                              confidence=0.80,
                              summary="Jobs are processed asynchronously in a concurrent pool.")
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is not None
        assert result.kind == ConflictKind.content_contradiction

    def test_no_contradiction_when_consistent(self):
        existing = make_node(title="Retry Logic", kind="architecture",
                             summary="Retry uses exponential backoff for transient errors.")
        cand = make_candidate(title="Retry Logic", kind=MemoryKind.architecture,
                              confidence=0.80,
                              summary="Retry policy: exponential backoff on transient HTTP 5xx.")
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is None


class TestStaleExistingNoConflict:
    def test_stale_existing_allows_supersede(self):
        existing = make_node(title="Old Architecture", kind="architecture",
                             confidence=0.95, status="stale")
        cand = make_candidate(title="Old Architecture", kind=MemoryKind.architecture,
                              confidence=0.70)
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is None

    def test_superseded_existing_no_conflict(self):
        existing = make_node(title="Superseded Decision", kind="decision",
                             confidence=0.99, status="superseded")
        cand = make_candidate(title="Superseded Decision", kind=MemoryKind.decision,
                              confidence=0.50)
        result = svc.evaluate(cand, _match(existing, cand))
        assert result is None
