"""DeterministicRanker — explicit, testable, no black-box framework.

Phase 4 score formula (weights sum to 1.0):

  final_score =
    0.20 * semantic_similarity      (Phase 4: placeholder = lexical; real embeddings in future)
  + 0.15 * lexical_similarity       (Jaccard word overlap: task vs title+summary+tags)
  + 0.10 * module_path_overlap      (node.module_path parts vs current_files stems)
  + 0.05 * symbol_overlap           (node.tags vs current_symbols)
  + 0.10 * task_intent_compatibility (affinity table: intent × MemoryKind)
  + 0.15 * tree_proximity           (inverse depth: 1/(1 + depth*0.2))
  + 0.10 * importance               (node.importance, already in [0,1])
  + 0.10 * confidence               (node.confidence, already in [0,1])
  + 0.05 * freshness                (decay from updated_at, min 0.2)

semantic_similarity is a placeholder for future embedding-based scoring.
It is currently computed identically to lexical_similarity so it can be
swapped out when an EmbeddingProvider is available.

All component scores are in [0, 1].
The breakdown is stored for every scored node so retrieval traces are exact.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from memory_engine.models.domain import (
    MemoryKind,
    MemoryNode,
    ScoredMemory,
    TaskIntent,
)

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

# Phase 4 weights — semantic_similarity added as first-class signal
W_SEMANTIC = 0.20   # placeholder = lexical; swap for real embeddings later
W_LEXICAL = 0.15
W_MODULE = 0.10
W_SYMBOL = 0.05
W_INTENT_COMPAT = 0.10
W_TREE_PROX = 0.15
W_IMPORTANCE = 0.10
W_CONFIDENCE = 0.10
W_FRESHNESS = 0.05

# ---------------------------------------------------------------------------
# Intent × MemoryKind affinity table
# ---------------------------------------------------------------------------

_AFFINITY: dict[TaskIntent, dict[MemoryKind, float]] = {
    TaskIntent.bug_fix: {
        MemoryKind.debug: 1.00,
        MemoryKind.constraint: 0.90,
        MemoryKind.decision: 0.80,
        MemoryKind.module: 0.70,
        MemoryKind.procedure: 0.60,
        MemoryKind.architecture: 0.40,
        MemoryKind.outcome: 0.30,
    },
    TaskIntent.test_failure: {
        MemoryKind.debug: 1.00,
        MemoryKind.procedure: 0.90,
        MemoryKind.constraint: 0.80,
        MemoryKind.module: 0.60,
        MemoryKind.decision: 0.50,
        MemoryKind.architecture: 0.30,
        MemoryKind.outcome: 0.40,
    },
    TaskIntent.refactor: {
        MemoryKind.architecture: 1.00,
        MemoryKind.constraint: 0.90,
        MemoryKind.decision: 0.85,
        MemoryKind.module: 0.80,
        MemoryKind.debug: 0.60,
        MemoryKind.procedure: 0.40,
        MemoryKind.outcome: 0.30,
    },
    TaskIntent.feature_implementation: {
        MemoryKind.module: 1.00,
        MemoryKind.architecture: 0.80,
        MemoryKind.constraint: 0.85,
        MemoryKind.procedure: 0.70,
        MemoryKind.decision: 0.65,
        MemoryKind.debug: 0.30,
        MemoryKind.outcome: 0.40,
    },
    TaskIntent.architecture_review: {
        MemoryKind.architecture: 1.00,
        MemoryKind.module: 0.70,
        MemoryKind.constraint: 0.60,
        MemoryKind.decision: 0.50,
        MemoryKind.procedure: 0.20,
        MemoryKind.debug: 0.20,
        MemoryKind.outcome: 0.30,
    },
    TaskIntent.code_explanation: {
        MemoryKind.architecture: 0.90,
        MemoryKind.module: 1.00,
        MemoryKind.decision: 0.70,
        MemoryKind.constraint: 0.60,
        MemoryKind.procedure: 0.50,
        MemoryKind.debug: 0.40,
        MemoryKind.outcome: 0.50,
    },
    TaskIntent.repository_onboarding: {
        MemoryKind.architecture: 1.00,
        MemoryKind.module: 0.80,
        MemoryKind.constraint: 0.75,
        MemoryKind.procedure: 0.70,
        MemoryKind.decision: 0.50,
        MemoryKind.debug: 0.30,
        MemoryKind.outcome: 0.40,
    },
    TaskIntent.workflow_question: {
        MemoryKind.procedure: 1.00,
        MemoryKind.architecture: 0.60,
        MemoryKind.module: 0.50,
        MemoryKind.constraint: 0.40,
        MemoryKind.decision: 0.30,
        MemoryKind.debug: 0.20,
        MemoryKind.outcome: 0.30,
    },
    TaskIntent.trivial_edit: {k: 0.1 for k in MemoryKind},
    TaskIntent.unknown: {k: 0.5 for k in MemoryKind},
}

# Default for intents not in table
_DEFAULT_AFFINITY: dict[MemoryKind, float] = {k: 0.5 for k in MemoryKind}

# Freshness half-life in days: at 180 days a node scores ~0.5 freshness
_FRESHNESS_HALF_LIFE = 180.0


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _word_set(text: str) -> frozenset[str]:
    """Lower-cased words longer than 2 characters."""
    return frozenset(w.lower() for w in re.findall(r"\w+", text) if len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


def _file_stems(files: list[str]) -> frozenset[str]:
    """Extract lowercase alphanumeric tokens from file paths."""
    tokens: set[str] = set()
    for f in files:
        stem = Path(f).stem.lower()
        tokens.update(re.findall(r"\w+", stem))
    return frozenset(t for t in tokens if len(t) > 2)


def _freshness(updated_at: datetime) -> float:
    """Exponential decay: 1.0 at update time, ~0.5 at half-life, min 0.2."""
    now = datetime.now(timezone.utc)
    # Ensure updated_at is tz-aware
    if updated_at.tzinfo is None:
        from datetime import timezone as tz

        updated_at = updated_at.replace(tzinfo=tz.utc)
    age_days = max(0.0, (now - updated_at).total_seconds() / 86400.0)
    score = 2.0 ** (-age_days / _FRESHNESS_HALF_LIFE)
    return max(0.2, score)


# ---------------------------------------------------------------------------
# DeterministicRanker
# ---------------------------------------------------------------------------


class DeterministicRanker:
    """Scores memory nodes against a task request using explicit signals only."""

    def score(
        self,
        node: MemoryNode,
        *,
        task: str,
        intent: TaskIntent,
        current_files: list[str],
        current_symbols: list[str],
    ) -> ScoredMemory:
        """Compute and return a ScoredMemory with a full breakdown dict."""
        task_words = _word_set(task)
        node_words = _word_set(
            f"{node.title} {node.summary} {' '.join(node.tags)}"
        )

        # 1. Lexical similarity (Jaccard)
        lex = _jaccard(task_words, node_words)

        # 2. Semantic similarity — Phase 4 placeholder (= lexical until embeddings added)
        #    When an EmbeddingProvider is injected, replace this line only.
        semantic = lex

        # 3. Module path overlap
        mod = self._module_overlap(node, current_files)

        # 4. Symbol overlap
        sym = self._symbol_overlap(node, current_symbols)

        # 5. Intent compatibility
        affinity_map = _AFFINITY.get(intent, _DEFAULT_AFFINITY)
        try:
            kind = MemoryKind(node.kind)
        except ValueError:
            kind = MemoryKind.module
        compat = affinity_map.get(kind, 0.5)

        # 6. Tree proximity
        prox = 1.0 / (1.0 + node.depth * 0.2)

        # 7. Importance (stored on node)
        imp = float(node.importance)

        # 8. Confidence (stored on node)
        conf = float(node.confidence)

        # 9. Freshness
        fresh = _freshness(node.updated_at)

        final = (
            W_SEMANTIC * semantic
            + W_LEXICAL * lex
            + W_MODULE * mod
            + W_SYMBOL * sym
            + W_INTENT_COMPAT * compat
            + W_TREE_PROX * prox
            + W_IMPORTANCE * imp
            + W_CONFIDENCE * conf
            + W_FRESHNESS * fresh
        )

        breakdown = {
            "semantic_similarity": round(semantic, 4),
            "lexical_similarity": round(lex, 4),
            "module_path_overlap": round(mod, 4),
            "symbol_overlap": round(sym, 4),
            "task_intent_compatibility": round(compat, 4),
            "tree_proximity": round(prox, 4),
            "importance": round(imp, 4),
            "confidence": round(conf, 4),
            "freshness": round(fresh, 4),
            "final_score": round(final, 4),
        }

        return ScoredMemory(node=node, score=round(final, 4), score_breakdown=breakdown)

    def rank(
        self,
        nodes: list[MemoryNode],
        *,
        task: str,
        intent: TaskIntent,
        current_files: list[str],
        current_symbols: list[str],
    ) -> list[ScoredMemory]:
        """Score all nodes and return them sorted by descending score."""
        scored = [
            self.score(
                n,
                task=task,
                intent=intent,
                current_files=current_files,
                current_symbols=current_symbols,
            )
            for n in nodes
        ]
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Component helpers
    # ------------------------------------------------------------------

    def _module_overlap(self, node: MemoryNode, current_files: list[str]) -> float:
        if not node.module_path or not current_files:
            return 0.0
        node_parts = frozenset(
            p.lower() for p in re.findall(r"\w+", node.module_path) if len(p) > 2
        )
        file_parts = _file_stems(current_files)
        if not node_parts:
            return 0.0
        return len(node_parts & file_parts) / len(node_parts)

    def _symbol_overlap(self, node: MemoryNode, current_symbols: list[str]) -> float:
        if not node.tags or not current_symbols:
            return 0.0
        node_tags = frozenset(t.lower() for t in node.tags)
        syms = frozenset(s.lower() for s in current_symbols)
        if not syms:
            return 0.0
        return len(node_tags & syms) / len(syms)
