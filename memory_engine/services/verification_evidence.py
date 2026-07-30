"""VerificationEvidenceService — verification-evidence levels (Issue 4).

Problem: an agent's own post-task report (``ReflectionInput.verification_status
== tests_passed`` / ``build_success`` / ``manual_check``) was, before this
module existed, treated as a fact on the same footing as if Memory Engine had
observed it directly. An agent *claiming* tests passed is not the same as the
system independently observing it — and a compromised or confused agent can
claim anything. This module adds an explicit "how much should this
verification claim be trusted" axis, independent of (and never a
replacement for) Issue 1 (source validity), Issue 2 (constraint scope), and
Issue 3 (source trust).

Design (mirrors ``source_trust.py`` and ``source_validity.py``):
  - Assigned at creation time, conservatively, from what the creating
    pipeline actually knows. Legacy ``VerificationStatus`` values
    (``tests_passed``, ``build_success``, ``manual_check``) — an agent's own
    self-report with no structured, independently-checkable evidence — map
    to ``VerificationEvidenceLevel.agent_claimed``, never anything stronger.
  - Stronger levels (``engine_observed``, ``external_observed``) require the
    caller to *explicitly* assert them alongside matching structured
    evidence (``VerificationEvidence``) through the correct API — an
    ``agent_claimed``-shaped call can never accidentally become stronger.
  - ``human_confirmed`` is reachable ONLY via ``apply_verification_transition``
    (mirrors Issue 3's ``apply_trust_transition``) — an explicit, auditable
    elevation action. It is never assignable at node-creation time.
  - ``engine_observed`` requires Memory Engine to have itself executed and
    observed a command (a trusted first-party execution path). No such path
    exists anywhere in this codebase today, and Issue 4 explicitly forbids
    adding shell-execution code in this session (rule 8). This level is
    therefore documented as **currently unreachable in production** — the
    enum value and its plumbing exist so a future, explicitly-designed
    execution-observation feature has somewhere to slot in, exactly like
    Issue 3 documented ``human_confirmed_policy`` as unreachable except via
    explicit elevation before this session existed to grant one legitimate
    path. Tests exercise it only via the same explicit-assertion API a
    hypothetical trusted caller would use.
  - Every assignment and every transition is auditable — same convention as
    ``set_validity``/``set_trust``: previous value, reason, actor, timestamp.
  - Legacy nodes (``evidence_level`` is NULL) default to
    ``VerificationEvidenceLevel.unverified`` — never fabricated as something
    stronger.

Staleness (conservative, cheap, bounded — mirrors ``SourceValidityService``):
  Stored evidence can reference a branch/commit that no longer matches the
  current context. Rather than deleting the record, ``effective_level()``
  downgrades the *effective* level returned for eligibility/confidence
  purposes when:
    - the evidence's ``source_branch`` is set and differs from the caller's
      current branch (when both are known);
    - the evidence's ``source_commit`` is set and differs from the caller's
      current commit (when both are known);
    - the node's own source-validity status (Issue 1) has already moved to
      ``needs_revalidation``/``invalidated`` — a memory whose source is no
      longer valid should not keep citing old verification as current.
  This never mutates the stored record (auditable, like Issue 1/3) — only
  the return value of ``effective_level()`` changes. Checks are cheap field
  comparisons against data already available via the caller-supplied
  ``current_branch``/``current_commit`` (from the existing, cached
  ``GitContext``/``GitContextResolver`` — Phase 9) — no new Git calls, no
  history scans, consistent with Issue 1's bounded-checks precedent.

  Not wired in this session (documented, not faked): working-tree-*content*
  level staleness (e.g. hashing the specific files verification ran
  against). Only branch/commit and Issue-1 source-validity status are used.
  A full working-tree-content hook would need a defined "what file(s) does
  this verification evidence cover" contract that does not exist yet;
  inventing one here would risk exactly the kind of speculative field this
  issue's spec explicitly warns against ("do not invent fields nobody will
  populate").

Confidence integration:
  ``confidence_adjustment()`` returns a small, bounded delta (never a
  multiplier that could invert ordering) applied on top of whatever
  confidence Issues 1-3's gates already allow. It is deliberately small and
  monotonic in evidence level so it can never let a memory bypass a gate it
  currently fails — see ``adjusted_confidence()``.
"""

from __future__ import annotations

from memory_engine.models.domain import (
    MemoryNode,
    MemoryStatus,
    VERIFICATION_EVIDENCE_ORDER,
    VerificationEvidence,
    VerificationEvidenceLevel,
    VerificationStatus,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository

__all__ = [
    "legacy_status_to_evidence_level",
    "assign_creation_evidence_level",
    "effective_evidence_level",
    "evidence_rank",
    "apply_verification_transition",
    "confidence_adjustment",
    "adjusted_confidence",
]

# Statuses (Issue 1) that indicate the node's underlying source evidence is
# no longer trustworthy — old verification claims tied to that source must
# not keep being read at full strength.
_SOURCE_STALE_STATUSES = frozenset({
    MemoryStatus.needs_revalidation,
    MemoryStatus.invalidated,
})

# Levels that require structured evidence to be present at all before they
# may be explicitly asserted at creation time. human_confirmed is
# deliberately excluded — creation is never a sanctioned path to it.
_CREATION_ASSERTABLE_LEVELS = frozenset({
    VerificationEvidenceLevel.engine_observed,
    VerificationEvidenceLevel.external_observed,
})

# Small, bounded, monotonic confidence deltas per evidence level. Never large
# enough to let a memory cross a gate (e.g. constraint_scope's
# _MIN_GLOBAL_CONFIDENCE) purely on the strength of verification evidence.
_CONFIDENCE_ADJUSTMENT: dict[str, float] = {
    VerificationEvidenceLevel.unverified.value: -0.05,
    VerificationEvidenceLevel.agent_claimed.value: 0.0,
    VerificationEvidenceLevel.external_observed.value: 0.02,
    VerificationEvidenceLevel.engine_observed.value: 0.03,
    VerificationEvidenceLevel.human_confirmed.value: 0.05,
}

# Legacy VerificationStatus -> conservative default evidence level. An
# agent's own self-report (tests_passed / build_success / manual_check) is
# agent_claimed — never stronger. unverified/tests_failed carry no positive
# verification claim at all.
_LEGACY_STATUS_TO_LEVEL: dict[VerificationStatus, VerificationEvidenceLevel] = {
    VerificationStatus.tests_passed: VerificationEvidenceLevel.agent_claimed,
    VerificationStatus.build_success: VerificationEvidenceLevel.agent_claimed,
    VerificationStatus.manual_check: VerificationEvidenceLevel.agent_claimed,
    VerificationStatus.unverified: VerificationEvidenceLevel.unverified,
    VerificationStatus.tests_failed: VerificationEvidenceLevel.unverified,
}


def legacy_status_to_evidence_level(
    status: VerificationStatus | str,
) -> VerificationEvidenceLevel:
    """Deterministic mapping used for every legacy caller.

    Never returns anything stronger than ``agent_claimed`` — this is the
    Issue 4 requirement that ``tests_passed=True``/``build_success=True``
    (i.e. these ``VerificationStatus`` values) are not treated as
    independently verified facts.
    """
    try:
        status = VerificationStatus(status)
    except ValueError:
        return VerificationEvidenceLevel.unverified
    return _LEGACY_STATUS_TO_LEVEL.get(status, VerificationEvidenceLevel.unverified)


def evidence_rank(level: VerificationEvidenceLevel | str) -> int:
    value = level.value if isinstance(level, VerificationEvidenceLevel) else level
    return VERIFICATION_EVIDENCE_ORDER.get(
        value, VERIFICATION_EVIDENCE_ORDER[VerificationEvidenceLevel.unverified.value]
    )


def assign_creation_evidence_level(
    *,
    verification_status: VerificationStatus | str,
    asserted_level: VerificationEvidenceLevel | str | None = None,
    evidence: VerificationEvidence | None = None,
) -> VerificationEvidenceLevel:
    """Conservative default evidence-level assignment at creation time.

    Returns the legacy-derived default unless the caller explicitly asserts
    ``engine_observed`` or ``external_observed`` AND supplies structured
    evidence to back it — an ``agent_claimed``-only call (the legacy shape)
    can never silently become stronger just by wording. ``human_confirmed``
    asserted here is never honored (falls back to the legacy default) — the
    only sanctioned path to it is ``apply_verification_transition``.
    """
    default = legacy_status_to_evidence_level(verification_status)

    if asserted_level is None or evidence is None:
        return default

    try:
        asserted = VerificationEvidenceLevel(asserted_level)
    except ValueError:
        return default

    if asserted in _CREATION_ASSERTABLE_LEVELS:
        return asserted

    return default


def effective_evidence_level(
    node: MemoryNode,
    *,
    current_branch: str | None = None,
    current_commit: str | None = None,
) -> VerificationEvidenceLevel:
    """Resolve the evidence level actually used for confidence/eligibility
    decisions, applying conservative, cheap staleness downgrades.

    Mirrors ``source_trust.effective_trust``'s "never fabricate, default
    down" convention: a node with no ``evidence_level`` resolves to
    ``VerificationEvidenceLevel.unverified``.
    """
    if node.evidence_level:
        try:
            stored = VerificationEvidenceLevel(node.evidence_level)
        except ValueError:
            return VerificationEvidenceLevel.unverified
    else:
        return VerificationEvidenceLevel.unverified

    # unverified/agent_claimed have no structured evidence to go stale.
    if stored in (VerificationEvidenceLevel.unverified, VerificationEvidenceLevel.agent_claimed):
        return stored

    # Issue 1 integration: source already known to be stale/invalid ->
    # verification evidence tied to that source can no longer be trusted at
    # its original level. Invalidated source -> unverified (no confidence
    # that the fact is even still true); needs_revalidation -> agent_claimed
    # (still plausible, but no longer independently corroborated).
    if node.status == MemoryStatus.invalidated:
        return VerificationEvidenceLevel.unverified
    if node.status == MemoryStatus.needs_revalidation:
        return VerificationEvidenceLevel.agent_claimed

    evidence = node.verification_evidence

    if evidence is not None:
        if (
            evidence.source_branch
            and current_branch
            and evidence.source_branch != current_branch
        ):
            return VerificationEvidenceLevel.agent_claimed
        if (
            evidence.source_commit
            and current_commit
            and evidence.source_commit != current_commit
        ):
            return VerificationEvidenceLevel.agent_claimed

    return stored


def apply_verification_transition(
    nodes_repo: MemoryNodeRepository,
    node: MemoryNode,
    *,
    new_level: VerificationEvidenceLevel,
    actor: str,
    reason: str,
) -> MemoryNode | None:
    """Explicitly change a node's verification-evidence level, with a full
    DB-backed audit trail — the Issue 4 analogue of
    ``source_trust.apply_trust_transition``.

    This is the only sanctioned path to
    ``VerificationEvidenceLevel.human_confirmed`` — requires an explicit
    caller-supplied ``actor``/``reason`` (a human or an explicit review
    process), never an automatic inference from a claim.

    Returns None (no-op) when ``new_level`` equals the node's current
    effective level — no fabricated transition is recorded for a no-op call.
    """
    current = effective_evidence_level(node)
    if new_level == current:
        return None

    elevated = evidence_rank(new_level) > evidence_rank(current)
    updated = nodes_repo.set_evidence_level(
        str(node.id),
        new_level=new_level.value,
        reason=reason,
        actor=actor,
        elevated=elevated,
    )
    if updated is None:
        return None
    return MemoryNode.model_validate(updated)


def confidence_adjustment(level: VerificationEvidenceLevel | str) -> float:
    value = level.value if isinstance(level, VerificationEvidenceLevel) else level
    return _CONFIDENCE_ADJUSTMENT.get(value, 0.0)


def adjusted_confidence(
    node: MemoryNode,
    *,
    current_branch: str | None = None,
    current_commit: str | None = None,
) -> float:
    """Read-time confidence signal incorporating verification-evidence level.

    Deliberately does NOT mutate ``node.confidence`` — this is an additional,
    independent read-time signal (Issue 4 rule: integrate without
    overriding Issues 1-3's gates). Callers that want verification level to
    influence eligibility should consult this in addition to — never instead
    of — the existing scope/trust/validity gates.
    """
    level = effective_evidence_level(
        node, current_branch=current_branch, current_commit=current_commit
    )
    return min(1.0, max(0.0, node.confidence + confidence_adjustment(level)))
