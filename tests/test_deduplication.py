"""Tests for DeduplicationService — Phase 3."""
import pytest

from memory_engine.models.domain import MemoryKind
from memory_engine.services.deduplication import (
    DeduplicationService,
    NEAR_DUPLICATE_THRESHOLD,
    PARTIAL_MATCH_THRESHOLD,
)
from tests.helpers import make_candidate, make_node

svc = DeduplicationService()


class TestNearDuplicate:
    def test_identical_title_same_kind_is_near_duplicate(self):
        existing = make_node(title="Scheduler Job Queue", kind="module",
                             module_path="scheduler.queue")
        cand = make_candidate(title="Scheduler Job Queue", kind=MemoryKind.module,
                              module_path="scheduler.queue")
        matches = svc.find_duplicates(cand, [existing])
        assert matches
        assert matches[0].similarity_score >= NEAR_DUPLICATE_THRESHOLD
        assert svc.is_near_duplicate(matches[0])

    def test_same_title_different_kind_not_near_duplicate(self):
        existing = make_node(title="Authentication Architecture", kind="architecture")
        cand = make_candidate(title="Authentication Architecture", kind=MemoryKind.module)
        matches = svc.find_duplicates(cand, [existing])
        assert matches
        assert matches[0].similarity_score < NEAR_DUPLICATE_THRESHOLD

    def test_very_similar_title_same_kind_near_duplicate(self):
        # Exact same text → Jaccard = 1.0 → near duplicate
        existing = make_node(title="Retry exponential backoff", kind="module")
        cand = make_candidate(title="Retry exponential backoff", kind=MemoryKind.module)
        matches = svc.find_duplicates(cand, [existing])
        assert matches
        assert matches[0].similarity_score >= NEAR_DUPLICATE_THRESHOLD


class TestPartialMatch:
    def test_overlapping_keywords_gives_partial_match(self):
        existing = make_node(title="Database Connection Pooling", kind="module")
        cand = make_candidate(title="Database Connection Management", kind=MemoryKind.module)
        matches = svc.find_duplicates(cand, [existing])
        assert matches
        assert PARTIAL_MATCH_THRESHOLD <= matches[0].similarity_score < NEAR_DUPLICATE_THRESHOLD
        assert svc.is_partial_match(matches[0])


class TestNoMatch:
    def test_completely_different_nodes_not_matched(self):
        existing = make_node(title="PostgreSQL Write-Ahead Log", kind="decision")
        cand = make_candidate(title="Frontend React Component Tree", kind=MemoryKind.module)
        matches = svc.find_duplicates(cand, [existing])
        assert not matches

    def test_empty_existing_nodes_returns_empty(self):
        cand = make_candidate(title="Anything", kind=MemoryKind.architecture)
        matches = svc.find_duplicates(cand, [])
        assert matches == []


class TestModulePathBoost:
    def test_same_module_path_boosts_score(self):
        shared_path = "auth.tokens"
        n_with_path = make_node(title="Token Validation", kind="module",
                                module_path=shared_path)
        n_without_path = make_node(title="Token Validation", kind="module",
                                   module_path=None)

        cand = make_candidate(title="Token Validation Logic", kind=MemoryKind.module,
                              module_path=shared_path)

        matches_with = svc.find_duplicates(cand, [n_with_path])
        matches_without = svc.find_duplicates(cand, [n_without_path])

        score_with = matches_with[0].similarity_score if matches_with else 0.0
        score_without = matches_without[0].similarity_score if matches_without else 0.0

        assert score_with >= score_without
