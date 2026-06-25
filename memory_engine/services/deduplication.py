"""DeduplicationService — deterministic duplicate detection for memory candidates.

Strategy:
  composite_score = 0.6 * title_similarity
                  + 0.2 * (1.0 if same_kind else 0.0)
                  + 0.2 * module_path_overlap

  Thresholds:
    >= 0.8  → near-identical duplicate (merge / discard / supersede)
    >= 0.5  → partial overlap (update / review)
    <  0.5  → not a duplicate

Title similarity uses Jaccard over normalised word tokens.
An optional EmbeddingProvider interface placeholder is included;
Stage 3 uses lexical fallback only (no external API required).
"""

from __future__ import annotations

import re

from memory_engine.models.domain import DuplicateMatch, MemoryNode, PersistedCandidate

# Similarity thresholds
NEAR_DUPLICATE_THRESHOLD = 0.80
PARTIAL_MATCH_THRESHOLD = 0.50


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _word_set(text: str) -> frozenset[str]:
    """Lower-cased word tokens longer than 2 characters."""
    return frozenset(w.lower() for w in re.findall(r"\w+", text) if len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _path_tokens(path: str) -> frozenset[str]:
    return frozenset(p.lower() for p in re.findall(r"\w+", path) if len(p) > 2)


def _module_overlap(path_a: str | None, path_b: str | None) -> float:
    if not path_a or not path_b:
        return 0.0
    ta = _path_tokens(path_a)
    tb = _path_tokens(path_b)
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


# ---------------------------------------------------------------------------
# Similarity interface placeholder (Phase 3 uses lexical; later stages plug in vectors)
# ---------------------------------------------------------------------------


class EmbeddingProviderStub:
    """Stub that always signals 'not available' — causes fallback to lexical."""

    def available(self) -> bool:
        return False

    def similarity(self, a: str, b: str) -> float:  # noqa: ARG002
        raise NotImplementedError("No embedding provider in Phase 3")


_EMBEDDING_STUB = EmbeddingProviderStub()


# ---------------------------------------------------------------------------
# DeduplicationService
# ---------------------------------------------------------------------------


class DeduplicationService:
    """Finds candidate duplicates using deterministic lexical signals."""

    def __init__(
        self,
        embedding_provider: EmbeddingProviderStub | None = None,
    ) -> None:
        self._embedder = embedding_provider or _EMBEDDING_STUB

    def find_duplicates(
        self,
        candidate: PersistedCandidate,
        existing_nodes: list[MemoryNode],
    ) -> list[DuplicateMatch]:
        """Return a list of duplicate matches sorted by descending similarity."""
        matches: list[DuplicateMatch] = []

        cand_title_tokens = _word_set(candidate.title)
        cand_summary_tokens = _word_set(candidate.summary)

        for node in existing_nodes:
            node_title_tokens = _word_set(node.title)

            title_sim = _jaccard(cand_title_tokens, node_title_tokens)
            # Boost: include first 30 words of summary in extended title comparison
            extended_node = _word_set(node.title + " " + node.summary[:200])
            extended_cand = _word_set(candidate.title + " " + candidate.summary[:200])
            extended_sim = _jaccard(extended_cand, extended_node)

            # Take max of plain title vs extended overlap
            effective_title_sim = max(title_sim, extended_sim * 0.8)

            kind_match = candidate.proposed_kind == node.kind
            mod_overlap = _module_overlap(
                candidate.proposed_module_path, node.module_path
            )

            composite = (
                0.6 * effective_title_sim
                + 0.2 * (1.0 if kind_match else 0.0)
                + 0.2 * mod_overlap
            )

            if composite >= PARTIAL_MATCH_THRESHOLD:
                matches.append(
                    DuplicateMatch(
                        existing_node=node,
                        similarity_score=round(composite, 4),
                        title_similarity=round(effective_title_sim, 4),
                        module_overlap=round(mod_overlap, 4),
                        is_same_kind=kind_match,
                    )
                )

        matches.sort(key=lambda m: m.similarity_score, reverse=True)
        return matches

    def is_near_duplicate(self, match: DuplicateMatch) -> bool:
        return match.similarity_score >= NEAR_DUPLICATE_THRESHOLD

    def is_partial_match(self, match: DuplicateMatch) -> bool:
        return PARTIAL_MATCH_THRESHOLD <= match.similarity_score < NEAR_DUPLICATE_THRESHOLD
