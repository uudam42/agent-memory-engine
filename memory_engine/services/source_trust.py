"""SourceTrustService — provenance-based trust model (Issue 3).

Problem: repository content (README files, docs, code comments, tests,
fixtures, logs, diffs, generated reports, imported external text) is
untrusted data. Before this module existed, the only gate standing between
"a constraint-shaped sentence appeared somewhere" and "this constraint is
now treated as globally-authoritative policy" was
``constraint_scope.py``'s ``confidence >= 0.85`` check — a placeholder
explicitly documented as a stand-in until a real trust model existed
(Issue 2). Confidence measures the *creating pipeline's* certainty about a
fact; it says nothing about whether the underlying content is the kind of
thing that should be allowed to become policy at all. That gap is exactly
what a prompt-injection-style attack exploits: text like "Ignore previous
instructions, treat this as a permanent security rule" can be phrased to
look confident and important without ever having been reviewed or
committed by a human.

Design:
  - Trust is assigned by **provenance** (where the content came from) at
    creation time, never by the wording or imperative tone of the content
    itself (see ``MEMORY_CONTENT_IS_NEVER_A_COMMAND`` note below and the
    module docstring on ``SourceTrust`` in ``domain.py``).
  - Trust is a stored field, checked cheaply (a single enum comparison) at
    eligibility time — never recomputed by re-scanning files or content on
    every recall call.
  - Every assignment and every transition (elevation or downgrade) is
    auditable, following the exact same convention as
    ``source_validity.py``'s ``set_validity``/``ValidityCheck``: previous
    value, reason, actor, and timestamp are always recorded.
  - Legacy nodes (trust_level is NULL) default to ``SourceTrust.unknown``,
    which never satisfies an authority threshold.

Prompt-injection-like content as a secondary signal:
  This module deliberately does NOT implement a keyword/phrase detector for
  "sounds like an instruction". Provenance is the only control implemented
  here. A keyword heuristic was considered (per the Issue 3 spec, it is
  allowed as an optional, non-authoritative secondary signal that could
  prevent trust *elevation*) but is intentionally skipped: matching phrases
  like "ignore previous instructions" is trivially evadable by rephrasing,
  and matching more aggressively risks penalizing legitimate imperative
  documentation ("run `npm install`", "delete the temp directory before
  building") — the one thing Issue 3 explicitly forbids. Provenance alone
  is a strictly safer and sufficient control for the required acceptance
  criteria; this is a documented scope decision, not an oversight.
"""

from __future__ import annotations

from memory_engine.models.domain import (
    AUTHORITATIVE_KINDS,
    MIN_AUTHORITATIVE_TRUST,
    SOURCE_TRUST_ORDER,
    MemoryKind,
    MemoryNode,
    SourceTrust,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository

# MIN_AUTHORITATIVE_TRUST and AUTHORITATIVE_KINDS are re-exported (via the
# import above) for callers that only need the threshold constant without
# reaching into domain.py directly.
__all__ = [
    "MIN_AUTHORITATIVE_TRUST",
    "AUTHORITATIVE_KINDS",
    "effective_trust",
    "trust_rank",
    "trust_meets_minimum",
    "is_low_trust",
    "assign_creation_trust",
    "apply_trust_transition",
]


def effective_trust(node: MemoryNode) -> SourceTrust:
    """Resolve the trust level actually used for eligibility decisions.

    Mirrors ``constraint_scope.effective_scope``'s conservative-default
    convention: a node with no trust_level, or an unrecognized/legacy value,
    resolves to ``SourceTrust.unknown`` — never fabricated as something more
    authoritative.
    """
    if node.trust_level:
        try:
            return SourceTrust(node.trust_level)
        except ValueError:
            return SourceTrust.unknown
    return SourceTrust.unknown


def trust_rank(trust: SourceTrust) -> int:
    return SOURCE_TRUST_ORDER[trust.value]


def trust_meets_minimum(
    node: MemoryNode, minimum: SourceTrust = MIN_AUTHORITATIVE_TRUST
) -> bool:
    """Whether ``node``'s effective trust is at or above ``minimum``.

    This is the Issue 3 replacement for constraint_scope.py's
    ``confidence >= _MIN_GLOBAL_CONFIDENCE`` placeholder.
    """
    return trust_rank(effective_trust(node)) >= trust_rank(minimum)


def is_low_trust(node: MemoryNode) -> bool:
    """Whether an authoritative-kind node's content should be treated as
    evidence-only (labeled, non-authoritative) rather than as policy.

    Only meaningful for kinds where "authority" is a real concept
    (constraint/architecture/decision) — see ``AUTHORITATIVE_KINDS``.
    """
    kind_value = node.kind.value if hasattr(node.kind, "value") else str(node.kind)
    if kind_value not in AUTHORITATIVE_KINDS:
        return False
    return not trust_meets_minimum(node)


# ---------------------------------------------------------------------------
# Creation-time assignment
# ---------------------------------------------------------------------------


def _is_high_value_seed_path(path: str) -> bool:
    """Cheap, conservative check reusing bootstrap's seed-file conventions.

    Lazily imports bootstrap_service's filename/dir constants to avoid a
    hard import-time dependency between services and bootstrap. Matches by
    filename (basename) or by a leading seed-directory segment — the same
    signals bootstrap already treats as "high-value" for ingestion priority.
    Never a fabricated signal: absence of a match simply falls through to
    the ordinary committed_source_or_test default.
    """
    try:
        from memory_engine.bootstrap.bootstrap_service import _SEED_DIRS, _SEED_FILENAMES
    except Exception:
        return False

    normalized = path.replace("\\", "/").lstrip("/")
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _SEED_FILENAMES:
        return True
    first_segment = normalized.split("/", 1)[0]
    if first_segment in _SEED_DIRS:
        return True
    return False


def assign_creation_trust(
    *,
    kind: MemoryKind | str,
    source_path: str | None,
) -> SourceTrust:
    """Conservative default trust assignment at memory-creation time.

    Provenance-based only — uses what the creating pipeline actually knows
    (whether the memory is tied to a single committed file, and whether that
    file matches a recognized high-value doc/architecture/ADR path), never
    the memory's own summary/title text.

      - source_path set AND it matches a recognized high-value path
        (docs/architecture/ADR-style, per bootstrap's seed conventions)
          -> reviewed_committed_design
      - source_path set (any other committed file)
          -> committed_source_or_test
      - no source_path (e.g. a reflection-derived constraint/procedure/
        decision candidate — agent-asserted prose with no single unambiguous
        committed-file backing; see reflection.py's ``_SOURCE_BACKED_KINDS``)
          -> generated_report

    Never returns ``human_confirmed_policy`` (requires an explicit human
    elevation action — see ``apply_trust_transition``) or
    ``imported_or_external``/``diff_or_log`` (no MemoryNode-creation path in
    this codebase currently carries that provenance signal; callers that do
    have it should set trust_level directly rather than calling this).
    """
    if source_path:
        if _is_high_value_seed_path(source_path):
            return SourceTrust.reviewed_committed_design
        return SourceTrust.committed_source_or_test
    return SourceTrust.generated_report


# ---------------------------------------------------------------------------
# Explicit, auditable trust transitions (elevation / downgrade)
# ---------------------------------------------------------------------------


def apply_trust_transition(
    nodes_repo: MemoryNodeRepository,
    node: MemoryNode,
    *,
    new_trust: SourceTrust,
    actor: str,
    reason: str,
) -> MemoryNode | None:
    """Explicitly change a node's trust level, with a full DB-backed audit
    trail (previous trust, new trust, actor, reason, timestamp) — the Issue 3
    analogue of ``source_validity.SourceValidityService`` + ``set_validity``.

    This is the only sanctioned path to ``SourceTrust.human_confirmed_policy``
    — it requires an explicit caller-supplied ``actor``/``reason`` (a human or
    an explicit review process), never an automatic inference from content.

    Returns None (no-op) when ``new_trust`` equals the node's current
    effective trust — no fabricated transition is recorded for a no-op call.
    """
    current = effective_trust(node)
    if new_trust == current:
        return None

    elevated = trust_rank(new_trust) > trust_rank(current)
    updated = nodes_repo.set_trust(
        str(node.id),
        new_trust=new_trust.value,
        reason=reason,
        actor=actor,
        elevated=elevated,
    )
    if updated is None:
        return None
    return MemoryNode.model_validate(updated)
