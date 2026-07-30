"""ConflictDetectionService — explicit, retrieval-time conflict detection (Issue 5).

Problem: Phase 9's branch-affinity ranking changes which memory a task sees
first, but it never tells the agent that two *qualifying* memories actively
disagree. Example: ``main`` says "use REST for service communication",
``feature/grpc`` says "use gRPC for service communication" — the ranker
correctly boosts the gRPC memory on that branch, but silently hides the
material conflict with mainline. Selecting one candidate by score alone,
with no signal that an alternative exists and disagrees, is exactly what
this module fixes.

Design (mirrors Issue 1/3/4's "recompute at retrieval time, don't persist
derived state redundantly" convention — see ``source_validity.py``,
``source_trust.py``, ``verification_evidence.py`` module docstrings):

  - Purely retrieval-time and in-memory. No new tables, no new relation
    rows are written. Conflicts are recomputed on every ``recall()`` call
    from fields already loaded on the candidate list — the same convention
    used by Issues 1/3/4's "effective_*()" style read-time resolution.
  - Bounded: operates only on the small, already-gated candidate list a
    caller passes in (in RecallService, this is the *composed* — i.e.
    already source-validity / scope / relevance / trust gated — selection,
    never the full project memory table). No O(n^2) full-table scan; the
    only quadratic-shaped work here is grouping within that small list.
  - Deterministic: ``ConflictInfo.conflict_group_id`` is a stable hash of
    the sorted member ids, so two separate ``recall()`` calls over the
    same underlying data return the same group id.
  - Reuses the existing ``RelationType.contradicts``/``supersedes`` edges
    (if any are stored in ``memory_relations``) as a strong, explicit
    grouping signal, but does not require them — deterministic, bounded
    structural signals (shared ``source_symbol``/``source_path``/
    ``module_path``, or, for ``decision``-kind nodes with none of those, a
    conservative exact-match normalized-title key) are the fallback.
    Deliberately NOT implemented: any form of unrestricted natural-language
    contradiction/entailment detection across arbitrary text (Issue 5 rule
    8) — two memories with merely similar wording but no shared identity
    signal and no explicit relation are never flagged.

Eligibility for conflict *membership* (Issue 5 rules 9/10):
  A candidate may only participate in an active conflict when it is:
    - not stale/superseded/archived/needs_revalidation/invalidated/
      needs_review (Issue 1's non-authoritative statuses, plus the
      pre-existing needs_review status);
    - sufficiently trusted, for authoritative kinds (constraint/
      architecture/decision) — reuses Issue 3's
      ``source_trust.trust_meets_minimum`` threshold. Module/procedure/
      debug/outcome nodes have no meaningful "authority" concept (see
      ``AUTHORITATIVE_KINDS``) so they are not trust-gated here either —
      identical to how Issue 3 itself only labels authoritative kinds.
  A memory that fails either check may still appear in composed context
  (existing composer behavior for low-trust content is unchanged — it can
  be labeled UNTRUSTED_REPOSITORY_CONTENT), but it can never be one side
  of an "active" conflict.

Grouping scope bounding (Issue 5 rules 12/3):
  Structural-identity grouping (symbol/path/module/decision-title) is
  bounded to candidates that are either unscoped/global, mainline-ish
  (``branch_scope`` in {global, mainline, inherited_branch}), or on the
  request's own ``current_branch``. A candidate on some *other*, unrelated
  branch never enters an identity-based group — it can only ever be pulled
  into a conflict via an explicit, already-stored ``contradicts``/
  ``supersedes`` relation (a deliberate, human/pipeline-created link),
  never merely by existing somewhere else in the database.

Resolution (Issue 5 rules 1-4):
  - Exactly one current-branch member + one-or-more mainline/global
    members, no unrelated-branch members -> ``current_branch_preferred``;
    the current-branch member is ``role="preferred"``, the rest
    ``role="historical"``.
  - Anything else (no branch context, two members on the same branch, an
    unrelated-branch member pulled in only via an explicit relation, etc.)
    -> ``unresolved`` — every member gets ``role="unresolved_peer"``. Score
    is never consulted to break this tie.
  - A stored ``supersedes`` relation between two eligible members removes
    the superseded (target) side from conflict membership entirely before
    grouping — it is not one of two live, disagreeing decisions, it is an
    explicitly-resolved lineage (defensive: normally the superseded node's
    ``status`` has already moved to ``superseded`` and would be excluded by
    the status gate anyway; this only matters if that transition has not
    yet been persisted).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Literal

from memory_engine.models.domain import (
    AUTHORITATIVE_KINDS,
    ConflictAlternativeRef,
    ConflictInfo,
    ConflictResolutionStatus,
    MemoryKind,
    MemoryNode,
    MemoryStatus,
    RelationType,
)
from memory_engine.services.source_trust import trust_meets_minimum

__all__ = ["detect_conflicts"]

# Statuses that make a node non-authoritative regardless of scope — reused
# from Issue 1/2's conventions (constraint_scope.py's
# _NON_AUTHORITATIVE_STATUSES, plus Phase 3's needs_review).
_EXCLUDED_STATUSES = frozenset({
    MemoryStatus.stale,
    MemoryStatus.superseded,
    MemoryStatus.archived,
    MemoryStatus.needs_revalidation,
    MemoryStatus.invalidated,
    MemoryStatus.needs_review,
})

# branch_scope values treated as "base/mainline-ish" for grouping-scope
# bounding purposes (Phase 9 vocabulary — see ranker.py's
# _BRANCH_SCOPE_PRIORITY).
_BASE_SCOPES = frozenset({"global", "mainline", "inherited_branch"})


def _identity_reason(key: str) -> str:
    if key.startswith("symbol:"):
        return "same_source_symbol"
    if key.startswith("path:"):
        return "same_source_path"
    if key.startswith("module:"):
        return "same_module_path"
    if key.startswith("decision_title:"):
        return "same_decision_topic"
    return "branch_scoped_decision_mismatch"


def _normalize_title(title: str) -> str:
    """Conservative, exact-match normalization — lowercase alnum tokens
    only. Not fuzzy matching: two titles must normalize to the identical
    string to be grouped, which is deliberately strict to avoid
    false-positive conflicts from merely-similar wording (Issue 5 rule 8).
    """
    words = re.findall(r"[a-z0-9]+", title.lower())
    return " ".join(words)


def _identity_key(node: MemoryNode) -> str | None:
    """Derive the most specific available identity signal for grouping.

    Priority (most specific first): source_symbol > source_path >
    module_path > (decision-kind only) normalized title. Returns None when
    no signal is available — such a node can still be pulled into a
    conflict via an explicit relation, but never via identity matching.
    """
    if node.source_symbol:
        return f"symbol:{node.source_symbol}"
    if node.source_path:
        return f"path:{node.source_path}"
    if node.module_path:
        return f"module:{node.module_path}"
    if node.kind == MemoryKind.decision:
        normalized = _normalize_title(node.title)
        if normalized:
            return f"decision_title:{normalized}"
    return None


def _is_eligible_member(node: MemoryNode) -> bool:
    if node.status in _EXCLUDED_STATUSES:
        return False
    kind_value = node.kind.value if hasattr(node.kind, "value") else str(node.kind)
    if kind_value in AUTHORITATIVE_KINDS and not trust_meets_minimum(node):
        return False
    return True


def _is_branch_bounded(node: MemoryNode, current_branch: str | None) -> bool:
    """Whether ``node`` may participate in *identity-based* grouping.

    Bounded to: unscoped/global nodes, mainline-ish nodes, or nodes on the
    request's own current branch. A node on a different, specific branch
    is excluded here — it can still join a conflict via an explicit
    relation (see ``detect_conflicts``), just never via shared identity
    alone (Issue 5 rule 12: unrelated feature branches are not pulled in
    by default).
    """
    if node.branch_name is None:
        return True
    scope = node.branch_scope or "global"
    if scope in _BASE_SCOPES:
        return True
    return current_branch is not None and node.branch_name == current_branch


def _classify(node: MemoryNode, current_branch: str | None) -> str:
    if current_branch is not None and node.branch_name == current_branch:
        return "current"
    scope = node.branch_scope or "global"
    if node.branch_name is None or scope in _BASE_SCOPES:
        return "mainline_or_global"
    return "other"


def _group_id(member_ids: Iterable[str]) -> str:
    """Deterministic id from the sorted set of member ids — stable across
    repeated calls with the same underlying data, never a random UUID."""
    basis = "|".join(sorted(member_ids))
    return "conflict_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


class _UnionFind:
    def __init__(self, ids: Iterable[str]) -> None:
        self._parent: dict[str, str] = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Deterministic direction (lexicographic) — doesn't affect final
        # membership, only which id becomes the internal root label.
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb


def detect_conflicts(
    candidates: list[MemoryNode],
    *,
    current_branch: str | None = None,
    relations: list[Any] | None = None,
) -> dict[str, ConflictInfo]:
    """Group ``candidates`` into active conflicts and return per-member metadata.

    ``candidates`` must already be the bounded, gated set for the current
    request (post source-validity / scope / relevance filtering — see
    ``RecallService.recall``); this function does not re-run those gates,
    it only additionally excludes non-authoritative-status and
    below-trust-threshold members from conflict *participation* (Issue 5
    rules 9/10).

    ``relations`` is an optional bounded list of relation-like objects
    (``source_id``, ``target_id``, ``relation_type`` attributes — e.g.
    ``MemoryRelationORM`` rows already restricted to this candidate set,
    such as ``RelationRepository.list_by_node_ids``'s result) used to
    detect explicit ``contradicts``/``supersedes`` links.

    Returns a dict keyed by ``str(memory_id)`` -> ``ConflictInfo``,
    containing an entry only for memories that are part of an active
    (size >= 2) conflict group. Memories with no conflict are simply
    absent from the returned dict.
    """
    node_by_id: dict[str, MemoryNode] = {str(n.id): n for n in candidates}
    eligible_ids = [nid for nid, n in node_by_id.items() if _is_eligible_member(n)]
    eligible_set = set(eligible_ids)

    if len(eligible_ids) < 2:
        return {}

    uf = _UnionFind(eligible_ids)
    edge_reason: dict[frozenset[str], str] = {}

    # -- 1. Explicit relation edges (bypass branch bounding) ----------------
    drop_ids: set[str] = set()
    for rel in relations or []:
        source_id = getattr(rel, "source_id", None)
        target_id = getattr(rel, "target_id", None)
        rel_type = getattr(rel, "relation_type", None)
        if source_id not in eligible_set or target_id not in eligible_set:
            continue
        if rel_type == RelationType.supersedes.value:
            # Defensive (see module docstring): the superseded side is not
            # a live, disagreeing alternative — drop it from membership.
            drop_ids.add(target_id)
        elif rel_type == RelationType.contradicts.value:
            uf.union(source_id, target_id)
            edge_reason[frozenset({source_id, target_id})] = "explicit_contradicts_relation"

    if drop_ids:
        eligible_ids = [i for i in eligible_ids if i not in drop_ids]
        eligible_set = set(eligible_ids)

    # -- 2. Structural identity grouping (branch-bounded) --------------------
    identity_groups: dict[str, list[str]] = {}
    for nid in eligible_ids:
        node = node_by_id[nid]
        if not _is_branch_bounded(node, current_branch):
            continue
        key = _identity_key(node)
        if key is None:
            continue
        identity_groups.setdefault(key, []).append(nid)

    for key, ids in identity_groups.items():
        if len(ids) < 2:
            continue
        reason = _identity_reason(key)
        base = ids[0]
        for other in ids[1:]:
            uf.union(base, other)
            edge_reason[frozenset({base, other})] = reason

    # -- 3. Assemble final groups (drop members removed by supersedes) ------
    groups: dict[str, list[str]] = {}
    for nid in eligible_ids:
        root = uf.find(nid)
        groups.setdefault(root, []).append(nid)

    conflict_map: dict[str, ConflictInfo] = {}

    for ids in groups.values():
        if len(ids) < 2:
            continue

        members = [node_by_id[i] for i in ids]
        id_set = set(ids)
        reasons = {r for edge, r in edge_reason.items() if edge <= id_set}
        reason = sorted(reasons)[0] if reasons else "branch_scoped_decision_mismatch"
        group_id = _group_id(ids)

        classes = {i: _classify(node_by_id[i], current_branch) for i in ids}
        current_ids = [i for i in ids if classes[i] == "current"]
        mainline_ids = [i for i in ids if classes[i] == "mainline_or_global"]
        other_ids = [i for i in ids if classes[i] == "other"]

        role_map: dict[str, Literal["preferred", "historical", "unresolved_peer"]]
        if (
            len(current_ids) == 1
            and len(mainline_ids) >= 1
            and not other_ids
            and len(current_ids) + len(mainline_ids) == len(ids)
        ):
            resolution_status = ConflictResolutionStatus.current_branch_preferred
            preferred_scope = "current_branch"
            role_map = {i: ("preferred" if i in current_ids else "historical") for i in ids}
        else:
            resolution_status = ConflictResolutionStatus.unresolved
            preferred_scope = None
            role_map = {i: "unresolved_peer" for i in ids}

        for m in members:
            mid = str(m.id)
            others = [o for o in members if str(o.id) != mid]
            alternatives = [
                ConflictAlternativeRef(
                    memory_id=str(o.id),
                    title=o.title,
                    branch_name=o.branch_name,
                    role=role_map[str(o.id)],
                )
                for o in others
            ]
            conflict_map[mid] = ConflictInfo(
                conflict_group_id=group_id,
                resolution_status=resolution_status,
                preferred_scope=preferred_scope,
                alternatives=alternatives,
                reason=reason,
                # Issue 6: surface this member's OWN role (already computed
                # above in role_map) so provenance consumers don't need to
                # re-derive it from ``alternatives``.
                own_role=role_map[mid],
            )

    return conflict_map
