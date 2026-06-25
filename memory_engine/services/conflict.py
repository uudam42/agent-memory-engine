"""ConflictService — evaluates whether a candidate conflicts with an existing node.

Conflict rules (applied in priority order):
  1. Low-confidence override blocked:
       candidate.confidence < existing.confidence - CONFIDENCE_TOLERANCE
       → ConflictKind.confidence_too_low
       → PromoteAction.needs_review

  2. Content contradiction detected:
       Candidate summary appears to contradict existing summary
       (detected by antonym / negation keyword analysis — no LLM required)
       → ConflictKind.content_contradiction
       → PromoteAction.needs_review

  3. Existing node is already stale or superseded:
       No conflict — the candidate can safely create a new active node
       → None (no conflict, proceed normally)

  4. No conflict detected:
       → None

CONFIDENCE_TOLERANCE:
  A candidate with confidence exactly equal to or higher than the existing node
  may update/supersede it.
  A candidate with confidence < existing - TOLERANCE is blocked.
"""

from __future__ import annotations

import re

from memory_engine.models.domain import (
    ConflictKind,
    ConflictReport,
    DuplicateMatch,
    MemoryStatus,
    PersistedCandidate,
)

# How much lower a candidate confidence may be before it is blocked
CONFIDENCE_TOLERANCE = 0.15

# Antonym / contradiction signal pairs
_CONTRADICTION_PAIRS: list[tuple[frozenset[str], frozenset[str]]] = [
    (frozenset({"must", "always", "required", "mandatory", "never"}),
     frozenset({"optional", "may", "can", "allowed", "sometimes"})),
    (frozenset({"synchronous", "blocking", "sequential"}),
     frozenset({"asynchronous", "async", "concurrent", "parallel"})),
    (frozenset({"immutable", "readonly", "constant"}),
     frozenset({"mutable", "writable", "modifiable"})),
    (frozenset({"deprecated", "removed", "deleted"}),
     frozenset({"active", "current", "supported"})),
    (frozenset({"atomic", "transaction", "rollback"}),
     frozenset({"eventual", "best-effort", "idempotent"})),
]


def _word_set(text: str) -> frozenset[str]:
    return frozenset(w.lower() for w in re.findall(r"\w+", text))


def _appears_contradictory(text_a: str, text_b: str) -> bool:
    """Return True if one text asserts something the other explicitly denies."""
    words_a = _word_set(text_a)
    words_b = _word_set(text_b)

    for positive_set, negative_set in _CONTRADICTION_PAIRS:
        # a uses positive-side words, b uses negative-side words (or vice versa)
        if (words_a & positive_set) and (words_b & negative_set):
            return True
        if (words_b & positive_set) and (words_a & negative_set):
            return True

    return False


class ConflictService:
    """Evaluates candidate-vs-existing conflicts and returns a ConflictReport or None."""

    def evaluate(
        self,
        candidate: PersistedCandidate,
        duplicate_match: DuplicateMatch,
    ) -> ConflictReport | None:
        existing = duplicate_match.existing_node

        # Rule 3: existing is already non-active → no conflict
        if existing.status in (
            MemoryStatus.stale,
            MemoryStatus.superseded,
            MemoryStatus.archived,
        ):
            return None

        # Rule 1: confidence check
        if candidate.confidence < existing.confidence - CONFIDENCE_TOLERANCE:
            return ConflictReport(
                kind=ConflictKind.confidence_too_low,
                existing_node=existing,
                candidate_confidence=candidate.confidence,
                existing_confidence=existing.confidence,
                reason=(
                    f"Candidate confidence {candidate.confidence:.2f} is more than "
                    f"{CONFIDENCE_TOLERANCE:.0%} below existing node confidence "
                    f"{existing.confidence:.2f}. Cannot overwrite without human review."
                ),
            )

        # Rule 2: content contradiction
        if _appears_contradictory(candidate.summary, existing.summary):
            return ConflictReport(
                kind=ConflictKind.content_contradiction,
                existing_node=existing,
                candidate_confidence=candidate.confidence,
                existing_confidence=existing.confidence,
                reason=(
                    "Candidate summary appears to contradict existing summary. "
                    "Detected opposing claims about behaviour/semantics. "
                    "Human review required before merging."
                ),
            )

        return None
