"""Scope-aware constraint eligibility (Issue 2, Phase 16).

Problem: every ``constraint`` memory node currently bypasses the topical
relevance gate unconditionally (see ``memory_engine.skills.recall``'s
``_passes_relevance_gate``, which intentionally never gates ``constraint``).
This means a payment-service integer-cents rule surfaces on an unrelated
renderer task, a Windows-encoding rule surfaces on a Linux-only task, and so
on — "constraint" was being treated as a proxy for "always relevant",
which is only true for a small minority of genuinely global rules.

This module adds a scope model on top of the existing structured fields
(module_path, source_path, source_symbol, branch_name, tags) rather than
duplicating them:

  global        — applies to every task in this project. Bypasses the
                  relevance gate only when additionally active, source-valid
                  (or explicitly human-confirmed — not modeled pre-Issue-3),
                  sufficiently confident, and branch-compatible.
  repository    — applies anywhere in this project, but is not asserted to
                  be universally true outside topic-relevant tasks. This is
                  the conservative default for legacy/unscoped constraints:
                  it preserves pre-Issue-2 visibility (still project-scoped,
                  never cross-project) without claiming global authority.
  branch        — applies only when node.branch_name matches the current
                  branch (or no branch context is available on either side).
  module        — applies only when the current task/query has structural
                  overlap with node.module_path (module_path_overlap > 0 in
                  the ranker's score breakdown).
  path          — applies only when node.source_path is one of the current
                  task's touched files.
  symbol        — applies only when the current task/query has structural
                  overlap with node.source_symbol (symbol_overlap > 0).
  task_intent   — applies only when the node is tagged "intent:<value>" and
                  <value> matches the current task's intent. A node with no
                  intent tag under this scope is conservatively excluded
                  (we cannot verify compatibility, so we do not assume it).
  needs_scope_review — could not be scoped safely; never bypasses the gate.

Conservative legacy migration rule (never inferred as global):
  1. exactly one reliable structural reference exists -> symbol > path >
     module (most specific reference wins);
  2. otherwise, if the node is explicitly branch-bound -> branch;
  3. otherwise -> repository (NOT needs_scope_review — repository keeps
     existing effective visibility for legacy data, matching "constraint
     always appeared before" without claiming global/cross-topic authority);
  4. 'global' is NEVER inferred — it is only ever explicit.
"""

from __future__ import annotations

from memory_engine.models.domain import ConstraintScope, MemoryNode, MemoryStatus

# Minimum confidence required for an explicitly-global constraint to bypass
# the relevance gate. A stand-in for "sufficiently trusted" until Issue 3
# (source trust model) exists; documented, not fabricated as a full trust
# system.
_MIN_GLOBAL_CONFIDENCE = 0.85

# Statuses that make a node non-authoritative regardless of scope.
_NON_AUTHORITATIVE_STATUSES = frozenset({
    MemoryStatus.stale,
    MemoryStatus.superseded,
    MemoryStatus.archived,
    MemoryStatus.needs_review,
    MemoryStatus.needs_revalidation,
    MemoryStatus.invalidated,
})


def infer_legacy_scope(node: MemoryNode) -> ConstraintScope:
    """Conservative scope inference for a constraint with no explicit scope.

    Never returns 'global' — see module docstring rule 4. Uses
    ``constraint_scope_ref``/``module_path`` — never ``source_path``/
    ``source_symbol``, which remain reserved for Issue 1 (source-validity)
    evidence and are never set on constraint-kind nodes (Phase 3A A1).
    """
    if node.constraint_scope_ref and node.module_path is None:
        return ConstraintScope.path
    if node.module_path:
        return ConstraintScope.module
    if node.branch_name and node.branch_scope == "current_branch":
        return ConstraintScope.branch
    return ConstraintScope.repository


def effective_scope(node: MemoryNode) -> ConstraintScope:
    """Resolve the scope actually used for eligibility decisions."""
    if node.constraint_scope:
        try:
            return ConstraintScope(node.constraint_scope)
        except ValueError:
            return ConstraintScope.needs_scope_review
    return infer_legacy_scope(node)


def _has_intent_tag(node: MemoryNode, intent: str | None) -> bool:
    if not intent:
        return False
    return f"intent:{intent}" in node.tags


def constraint_is_eligible(
    node: MemoryNode,
    *,
    module_path_overlap: float = 0.0,
    symbol_overlap: float = 0.0,
    current_files: list[str] | None = None,
    current_branch: str | None = None,
    task_intent: str | None = None,
) -> bool:
    """Decide whether ``node`` (a constraint, or other always-on-by-default
    kind) may bypass the topical relevance gate for the current request.

    Applies regardless of MemoryKind so a future 'security_rule' kind (not
    yet modeled — see Issue 2 spec) can reuse the same eligibility rules
    once it exists; callers are responsible for restricting which kinds are
    routed through this check.
    """
    scope = effective_scope(node)
    current_files = current_files or []

    if scope == ConstraintScope.needs_scope_review:
        return False

    if scope == ConstraintScope.global_:
        if node.status != MemoryStatus.active:
            return False
        if node.status in _NON_AUTHORITATIVE_STATUSES:
            return False
        if node.confidence < _MIN_GLOBAL_CONFIDENCE:
            return False
        # Branch restriction: an explicitly-global constraint bound to a
        # specific branch must still respect that branch when both sides
        # have branch context.
        if node.branch_name and current_branch and node.branch_name != current_branch:
            return False
        return True

    if scope == ConstraintScope.repository:
        # Already project-isolated by the query itself — repository scope
        # never claims cross-project or cross-topic authority beyond that.
        return True

    if scope == ConstraintScope.branch:
        if node.branch_name and current_branch and node.branch_name != current_branch:
            return False
        return True

    if scope == ConstraintScope.module:
        return module_path_overlap > 0.0

    if scope == ConstraintScope.path:
        return bool(node.constraint_scope_ref) and node.constraint_scope_ref in current_files

    if scope == ConstraintScope.symbol:
        return symbol_overlap > 0.0

    if scope == ConstraintScope.task_intent:
        return _has_intent_tag(node, task_intent)

    return False  # unknown scope value — fail closed, never fabricate eligibility


def infer_candidate_scope(
    *,
    module_path: str | None,
    touched_files: list[str] | None,
    touched_symbols: list[str] | None,
    branch_name: str | None,
    branch_explicit: bool,
) -> ConstraintScope:
    """Same conservative inference as ``infer_legacy_scope``, applied at
    candidate-creation time (ReflectionSkill) instead of read time, so newly
    created constraints are scoped from the start rather than always
    falling back to legacy inference.
    """
    files = [f for f in (touched_files or []) if f]
    symbols = [s for s in (touched_symbols or []) if s]

    if len(files) == 1 and len(symbols) == 1:
        return ConstraintScope.symbol
    if len(files) == 1 and module_path is None:
        return ConstraintScope.path
    if module_path:
        return ConstraintScope.module
    if branch_explicit and branch_name:
        return ConstraintScope.branch
    return ConstraintScope.repository
