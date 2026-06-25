"""PlacementService — deterministic tree placement for memory candidates.

Depth rules:
  0 — architecture (project-wide), constraint (global)
  1 — architecture (subsystem), module (subsystem-level)
  2 — module (detail / implementation level)
  3 — decision, debug (incident), procedure, outcome
  4 — evidence only (not a node; evidence stored in evidence table)

Parent resolution order:
  1. If candidate provides proposed_parent_id → validate depth compatibility → use it.
  2. If module_path given → find best-matching existing node by path overlap.
  3. For depth-0 candidates → no parent (root node).
  4. For depth-3 candidates without a module_path → look for the best-matching
     module node as parent.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    MemoryKind,
    MemoryNode,
    PersistedCandidate,
    PlacementDecision,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository

# ---------------------------------------------------------------------------
# Kind → natural depth mapping
# ---------------------------------------------------------------------------

_KIND_DEPTH: dict[MemoryKind, int] = {
    MemoryKind.architecture: 0,    # default — bumped to 1 for subsystem arch
    MemoryKind.constraint: 0,
    MemoryKind.module: 2,          # module detail; subsystem modules stay at 1
    MemoryKind.decision: 3,
    MemoryKind.debug: 3,
    MemoryKind.procedure: 3,
    MemoryKind.outcome: 3,
}

# Kinds that live at the root (no parent required)
_ROOT_KINDS: frozenset[MemoryKind] = frozenset(
    {MemoryKind.architecture, MemoryKind.constraint}
)


def _word_tokens(text: str) -> frozenset[str]:
    return frozenset(w.lower() for w in re.findall(r"\w+", text) if len(w) > 2)


def _path_tokens(path: str) -> frozenset[str]:
    return frozenset(p.lower() for p in re.findall(r"\w+", path) if len(p) > 2)


def _path_overlap(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    ta, tb = _path_tokens(a), _path_tokens(b)
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


class PlacementService:
    def __init__(self, session: Session) -> None:
        self._nodes = MemoryNodeRepository(session)

    def decide(
        self,
        candidate: PersistedCandidate,
        *,
        project_id: str,
    ) -> PlacementDecision:
        """Return the placement decision for a candidate."""

        kind = MemoryKind(candidate.proposed_kind)
        natural_depth = _KIND_DEPTH.get(kind, 2)

        # --- Explicit parent hint ----------------------------------------
        if candidate.proposed_parent_id is not None:
            parent = self._nodes.get(str(candidate.proposed_parent_id))
            if parent is not None:
                actual_depth = parent.depth + 1
                return PlacementDecision(
                    intended_depth=actual_depth,
                    parent_id=candidate.proposed_parent_id,
                    parent_title=parent.title,
                    placement_reason=(
                        f"Caller-supplied parent_id accepted; "
                        f"depth set to {actual_depth} (parent depth {parent.depth} + 1)."
                    ),
                )

        # --- Root placement (architecture / constraint) -------------------
        if kind in _ROOT_KINDS and not candidate.proposed_module_path:
            return PlacementDecision(
                intended_depth=0,
                parent_id=None,
                parent_title=None,
                placement_reason=(
                    f"Kind '{kind}' with no module_path → placed at depth 0 (project root)."
                ),
            )

        # --- Path-based parent resolution ---------------------------------
        all_nodes = [
            MemoryNode.model_validate(o)
            for o in self._nodes.list_by_project(project_id)
        ]

        best_parent = self._best_parent(
            candidate, kind, natural_depth, all_nodes
        )

        if best_parent is not None:
            actual_depth = best_parent.depth + 1
            return PlacementDecision(
                intended_depth=actual_depth,
                parent_id=best_parent.id,
                parent_title=best_parent.title,
                placement_reason=(
                    f"Module-path overlap resolved parent to '{best_parent.title}' "
                    f"(depth {best_parent.depth}); candidate depth={actual_depth}."
                ),
            )

        # --- Fallback: place at natural depth with no parent --------------
        return PlacementDecision(
            intended_depth=natural_depth,
            parent_id=None,
            parent_title=None,
            placement_reason=(
                f"No matching parent found; placed at natural depth {natural_depth} "
                f"for kind '{kind}' (root node)."
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _best_parent(
        self,
        candidate: PersistedCandidate,
        kind: MemoryKind,
        natural_depth: int,
        all_nodes: list[MemoryNode],
    ) -> MemoryNode | None:
        """Find the highest-overlap parent in the existing tree."""

        # For depth-3 kinds look for module/architecture parents (depth 0-2)
        # For depth-1/2 kinds look for architecture/module parents above them
        max_parent_depth = natural_depth - 1

        candidates_for_parent = [
            n for n in all_nodes
            if n.depth <= max_parent_depth
            and n.status in ("active", "stale")   # don't parent under archived
        ]

        if not candidates_for_parent:
            return None

        module_path = candidate.proposed_module_path or ""
        title_tokens = _word_tokens(candidate.title)

        best: MemoryNode | None = None
        best_score = 0.0

        for node in candidates_for_parent:
            path_score = _path_overlap(module_path, node.module_path or "")
            node_title_tokens = _word_tokens(node.title)
            title_score = (
                len(title_tokens & node_title_tokens) / max(len(node_title_tokens), 1)
            ) if node_title_tokens else 0.0

            # Prefer exact-kind parents for clustering
            kind_bonus = 0.1 if node.kind in ("architecture", "module") else 0.0
            score = 0.6 * path_score + 0.3 * title_score + 0.1 * kind_bonus

            if score > best_score:
                best_score = score
                best = node

        # Only accept a parent if similarity is meaningful
        return best if best_score >= 0.2 else None
