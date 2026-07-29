"""ReflectionSkill — decides what knowledge is worth retaining after a task.

Design principles:
  - Fully deterministic.  Same ReflectionInput → same ReflectionAnalysis.
  - No LLM, no user commands.  The agent calls this once after task completion.
  - Conservative by default: prefers not to create noise.
  - Generates the minimum set of candidates that capture genuine new knowledge.

Decision pipeline:
  1. Gate: skip if task failed / reverted / unverified-and-low-confidence.
  2. Gate: skip if outcome is trivial (short summary, no touched files, no
     new constraints/procedures, trivial task intent).
  3. Candidate generation:
       a. One incident (debug) candidate if task was a bug fix and succeeded.
       b. One constraint candidate per discovered_constraint string.
       c. One procedure candidate per discovered_procedure string.
       d. One module candidate if substantial module work was done and the
          outcome summary is meaningful.
       e. One decision candidate if the task involved an architectural choice.
  4. Confidence and importance are derived from verification status and
     task complexity.

Importance heuristic (0.0–1.0):
  - Constraint:     always 0.90+ (highest priority memory kind)
  - Incident:       0.85  (important context for future bug investigations)
  - Decision:       0.80
  - Procedure:      0.70
  - Module summary: 0.60

Confidence derived from VerificationStatus:
  - tests_passed  → 0.95
  - build_success → 0.85
  - manual_check  → 0.75
  - unverified    → 0.60
  - tests_failed  → do not create (gated out above)
"""

from __future__ import annotations

import re
from uuid import UUID

from memory_engine.models.domain import (
    CandidateCreate,
    ConstraintScope,
    MemoryKind,
    ReflectionAnalysis,
    ReflectionInput,
    ReflectionSkipReason,
    TaskIntent,
    TaskOutcome,
    VerificationStatus,
)
from memory_engine.services.constraint_scope import infer_candidate_scope

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Minimum agent_confidence when verification is "unverified"
_MIN_CONFIDENCE_UNVERIFIED = 0.70

# Minimum outcome_summary word count to be considered non-trivial
_MIN_SUMMARY_WORDS = 8

# VerificationStatus → candidate confidence
_VERIFICATION_CONFIDENCE: dict[VerificationStatus, float] = {
    VerificationStatus.tests_passed: 0.95,
    VerificationStatus.build_success: 0.85,
    VerificationStatus.manual_check: 0.75,
    VerificationStatus.unverified: 0.60,
    VerificationStatus.tests_failed: 0.0,   # not used (gated out)
}

# Intent → whether to create an incident record for bug fixes
_BUG_FIX_INTENTS: frozenset[TaskIntent] = frozenset({
    TaskIntent.bug_fix,
    TaskIntent.test_failure,
})

# Intent → whether an architectural / module candidate is appropriate
_STRUCTURAL_INTENTS: frozenset[TaskIntent] = frozenset({
    TaskIntent.feature_implementation,
    TaskIntent.refactor,
    TaskIntent.architecture_review,
})

# Intent → whether a decision candidate is appropriate
_DECISION_INTENTS: frozenset[TaskIntent] = frozenset({
    TaskIntent.feature_implementation,
    TaskIntent.refactor,
    TaskIntent.architecture_review,
})

# High-risk keyword patterns (same as router/ranker for consistency)
_HIGH_RISK_RE = re.compile(
    r"\b(state.?machine|lifecycle|terminal.?state|auth|authen|authori|"
    r"schema|migration|payment|billing|security|credential|secret|token|"
    r"permission|rbac|public.?api|breaking.?change|shared.?infra|"
    r"distributed|consensus|lock|transaction|rollback|idempoten)\b",
    re.I,
)


def _word_count(text: str) -> int:
    return len(text.split())


def _infer_intent(inp: ReflectionInput) -> TaskIntent:
    """Infer task intent if not explicitly provided."""
    if inp.task_intent is not None:
        return inp.task_intent

    task_lower = inp.task_description.lower()

    if any(w in task_lower for w in ("fix", "bug", "crash", "error", "broken")):
        return TaskIntent.bug_fix
    if any(w in task_lower for w in ("test fail", "flaky", "assertion")):
        return TaskIntent.test_failure
    if any(w in task_lower for w in ("refactor", "restructure", "clean up", "extract")):
        return TaskIntent.refactor
    if any(w in task_lower for w in ("add", "implement", "create", "build", "introduce", "support")):
        return TaskIntent.feature_implementation
    if any(w in task_lower for w in ("rename", "format", "whitespace", "typo", "comment", "lint")):
        return TaskIntent.trivial_edit
    return TaskIntent.unknown


def _derive_source_evidence(inp: ReflectionInput) -> tuple[str | None, str | None]:
    """Deterministically identify a single, unambiguous source file (Task 2).

    Returns (source_path, source_symbol).

    Conservative by design:
      - Exactly one touched file  -> that file becomes source_path (project-relative,
        as supplied by the caller).
      - Zero or 2+ touched files  -> source_path is left None. A reflection that
        summarizes changes across multiple files is not arbitrarily bound to the
        first one; the existing free-text `source_ref` field already records the
        full file list for human-readable provenance, but automatic hash/existence
        based staleness detection requires a single unambiguous file.
      - source_symbol is only ever set alongside a source_path, and only when
        exactly one touched_symbol was supplied — a symbol can't be meaningfully
        bound to an ambiguous file set.
    """
    files = [f for f in inp.touched_files if f]
    if len(files) != 1:
        return None, None
    source_path = files[0]

    symbols = [s for s in inp.touched_symbols if s]
    source_symbol = symbols[0] if len(symbols) == 1 else None
    return source_path, source_symbol


# Kinds treated as "strongly source-backed" (Task 7): an implementation fact
# or code pattern tied to a specific file, where automatic hash/existence
# invalidation is appropriate. Constraint, procedure, and decision candidates
# are treated as human-confirmed / repository-level policy — they should not
# become non-authoritative merely because one source file's hash drifted,
# so reflection never auto-binds source_path/source_symbol to them.
_SOURCE_BACKED_KINDS = frozenset({MemoryKind.debug, MemoryKind.module})


def _derive_tags(inp: ReflectionInput) -> list[str]:
    """Build a tag list from files, symbols, and module path."""
    tags: list[str] = []
    for sym in inp.touched_symbols[:5]:
        if sym not in tags:
            tags.append(sym)
    for f in inp.touched_files[:3]:
        stem = re.sub(r"[/\\.]", "_", f).rstrip("_py").lower()
        if stem and stem not in tags:
            tags.append(stem)
    if inp.module_path:
        parts = inp.module_path.split(".")
        tags.extend(p for p in parts if p not in tags)
    return tags[:8]


class ReflectionSkill:
    """Analyzes a completed task and generates MemoryCandidate objects.

    The agent calls `analyze()` once after task completion.
    No user interaction required.
    """

    def analyze(self, inp: ReflectionInput) -> ReflectionAnalysis:
        """Decide whether to retain knowledge and generate candidates."""

        # ── Gate 1: Task did not succeed ────────────────────────────────
        if inp.task_outcome in (TaskOutcome.failed, TaskOutcome.reverted):
            return ReflectionAnalysis(
                worth_retaining=False,
                skip_reason=(
                    ReflectionSkipReason.task_failed
                    if inp.task_outcome == TaskOutcome.failed
                    else ReflectionSkipReason.task_reverted
                ),
                retention_reasoning=[
                    f"Task outcome is '{inp.task_outcome}' — no verified knowledge to retain."
                ],
            )

        # ── Gate 2: Tests failed ─────────────────────────────────────────
        if inp.verification_status == VerificationStatus.tests_failed:
            return ReflectionAnalysis(
                worth_retaining=False,
                skip_reason=ReflectionSkipReason.task_failed,
                retention_reasoning=["Automated tests failed — outcome is unreliable."],
            )

        # ── Gate 3: Unverified + low confidence ──────────────────────────
        if (
            inp.verification_status == VerificationStatus.unverified
            and inp.agent_confidence < _MIN_CONFIDENCE_UNVERIFIED
        ):
            return ReflectionAnalysis(
                worth_retaining=False,
                skip_reason=ReflectionSkipReason.unverified_low_confidence,
                retention_reasoning=[
                    f"Verification status is 'unverified' and agent_confidence "
                    f"({inp.agent_confidence:.2f}) is below threshold "
                    f"({_MIN_CONFIDENCE_UNVERIFIED:.2f})."
                ],
            )

        # ── Gate 4: Trivial change ────────────────────────────────────────
        intent = _infer_intent(inp)
        has_special_signals = bool(
            inp.discovered_constraints
            or inp.discovered_procedures
            or _HIGH_RISK_RE.search(inp.task_description + " " + inp.outcome_summary)
        )

        if (
            intent == TaskIntent.trivial_edit
            and not has_special_signals
            and _word_count(inp.outcome_summary) < _MIN_SUMMARY_WORDS
        ):
            return ReflectionAnalysis(
                worth_retaining=False,
                skip_reason=ReflectionSkipReason.trivial_change,
                retention_reasoning=[
                    "Task classified as a trivial edit with no new constraints or "
                    "high-risk signals detected."
                ],
            )

        # ── Gate 5: Outcome summary too thin ─────────────────────────────
        if (
            _word_count(inp.outcome_summary) < _MIN_SUMMARY_WORDS
            and not has_special_signals
            and not inp.touched_files
        ):
            return ReflectionAnalysis(
                worth_retaining=False,
                skip_reason=ReflectionSkipReason.low_value,
                retention_reasoning=[
                    "Outcome summary is too brief and no additional signals detected."
                ],
            )

        # ── Build candidates ─────────────────────────────────────────────
        confidence = _VERIFICATION_CONFIDENCE.get(
            inp.verification_status, inp.agent_confidence
        )
        # Further clamp by agent's own self-assessment
        confidence = min(confidence, max(inp.agent_confidence, 0.60))

        candidates: list[CandidateCreate] = []
        reasoning: list[str] = []

        # Task 2/7: derive source evidence once — only applied to source-backed
        # kinds (debug/module) below. Never invented; absent when ambiguous.
        source_path, source_symbol = _derive_source_evidence(inp)

        # a) Discovered constraints
        # Issue 2: derive a conservative constraint_scope from the same
        # structured signals already available here (module_path / touched
        # files / touched symbols). Never global — a constraint discovered
        # during a single task is not thereby asserted to be universally
        # true; only an explicit, separately-reviewed process should mark a
        # constraint global (see constraint_scope.py docstring).
        proposed_scope = infer_candidate_scope(
            module_path=inp.module_path,
            touched_files=inp.touched_files,
            touched_symbols=inp.touched_symbols,
            branch_name=None,
            branch_explicit=False,
        )
        # For path scope, attach the same unambiguous single touched file as
        # a scope reference — via proposed_constraint_scope_ref, which is
        # deliberately independent of source_path/source_symbol (Task 7 /
        # Phase 3A A1: constraint candidates are never auto-bound to
        # source_path, since that field drives Issue 1's hash-drift
        # invalidation, which is inappropriate for human-confirmed policy
        # statements). Symbol scope relies on the symbol already being
        # present in proposed_tags (via _derive_tags) for eligibility
        # matching — no dedicated field needed.
        constraint_scope_ref = (
            source_path if proposed_scope == ConstraintScope.path else None
        )
        for constraint_text in inp.discovered_constraints:
            candidates.append(CandidateCreate(
                project_id=inp.project_id,
                title=_constraint_title(constraint_text),
                summary=constraint_text,
                proposed_kind=MemoryKind.constraint,
                proposed_tags=_derive_tags(inp),
                proposed_module_path=inp.module_path,
                source_ref=_source_ref(inp),
                confidence=min(confidence + 0.05, 1.0),  # constraints bump
                importance=0.92,
                evidence_content=inp.outcome_summary,
                evidence_source="post_task_reflection",
                proposed_constraint_scope=proposed_scope.value,
                proposed_constraint_scope_ref=constraint_scope_ref,
            ))
        if inp.discovered_constraints:
            reasoning.append(
                f"Created {len(inp.discovered_constraints)} constraint candidate(s) "
                f"from explicitly discovered constraints."
            )

        # b) Discovered procedures
        for proc_text in inp.discovered_procedures:
            candidates.append(CandidateCreate(
                project_id=inp.project_id,
                title=_procedure_title(proc_text),
                summary=proc_text,
                proposed_kind=MemoryKind.procedure,
                proposed_tags=_derive_tags(inp),
                proposed_module_path=inp.module_path,
                source_ref=_source_ref(inp),
                confidence=confidence,
                importance=0.72,
                evidence_content=inp.outcome_summary,
                evidence_source="post_task_reflection",
            ))
        if inp.discovered_procedures:
            reasoning.append(
                f"Created {len(inp.discovered_procedures)} procedure candidate(s) "
                f"from explicitly discovered procedures."
            )

        # c) Incident candidate (bug fix / test failure)
        if intent in _BUG_FIX_INTENTS and inp.task_outcome == TaskOutcome.completed:
            title = _incident_title(inp)
            candidates.append(CandidateCreate(
                project_id=inp.project_id,
                title=title,
                summary=inp.outcome_summary,
                proposed_kind=MemoryKind.debug,
                proposed_tags=_derive_tags(inp) + ["incident", "bug"],
                proposed_module_path=inp.module_path,
                source_ref=_source_ref(inp),
                confidence=confidence,
                importance=0.85,
                evidence_content=None,   # outcome_summary IS the evidence here
                evidence_source=None,
                source_path=source_path,
                source_symbol=source_symbol,
            ))
            reasoning.append(
                f"Created incident candidate: bug fix completed and verified "
                f"(verification={inp.verification_status})."
            )

        # d) Module summary candidate (structural work)
        if (
            intent in _STRUCTURAL_INTENTS
            and inp.task_outcome in (TaskOutcome.completed, TaskOutcome.partially_completed)
            and (inp.touched_files or inp.module_path)
            and _word_count(inp.outcome_summary) >= _MIN_SUMMARY_WORDS
        ):
            module_title = _module_title(inp)
            candidates.append(CandidateCreate(
                project_id=inp.project_id,
                title=module_title,
                summary=inp.outcome_summary,
                proposed_kind=MemoryKind.module,
                proposed_tags=_derive_tags(inp),
                proposed_module_path=inp.module_path,
                source_ref=_source_ref(inp),
                confidence=confidence,
                importance=0.62,
                evidence_content=None,
                evidence_source=None,
                source_path=source_path,
                source_symbol=source_symbol,
            ))
            reasoning.append(
                f"Created module candidate for structural work on "
                f"{inp.module_path or ', '.join(inp.touched_files[:2])}."
            )

        # e) Decision candidate (architectural choice)
        if (
            intent in _DECISION_INTENTS
            and inp.task_outcome == TaskOutcome.completed
            and _HIGH_RISK_RE.search(inp.task_description + " " + inp.outcome_summary)
        ):
            decision_title = _decision_title(inp)
            candidates.append(CandidateCreate(
                project_id=inp.project_id,
                title=decision_title,
                summary=inp.outcome_summary,
                proposed_kind=MemoryKind.decision,
                proposed_tags=_derive_tags(inp) + ["decision"],
                proposed_module_path=inp.module_path,
                source_ref=_source_ref(inp),
                confidence=confidence,
                importance=0.82,
                evidence_content=None,
                evidence_source=None,
            ))
            reasoning.append(
                "Created decision candidate: high-risk architectural change detected."
            )

        # f) Fallback: substantial verified work with changed files but unknown intent.
        #    Prevents "no_new_knowledge" for large implementation tasks whose titles
        #    don't match the standard intent keywords (e.g. "Phase N: ..." naming).
        _VERIFIED = (VerificationStatus.tests_passed, VerificationStatus.build_success)
        if (
            not candidates
            and intent == TaskIntent.unknown
            and inp.task_outcome == TaskOutcome.completed
            and inp.verification_status in _VERIFIED
            and len(inp.touched_files) >= 3
        ):
            candidates.append(CandidateCreate(
                project_id=inp.project_id,
                title=_module_title(inp),
                summary=inp.outcome_summary,
                proposed_kind=MemoryKind.module,
                proposed_tags=_derive_tags(inp),
                proposed_module_path=inp.module_path,
                source_ref=_source_ref(inp),
                confidence=confidence,
                importance=0.62,
                evidence_content=None,
                evidence_source=None,
                source_path=source_path,
                source_symbol=source_symbol,
            ))
            reasoning.append(
                f"Fallback module candidate: verified work on {len(inp.touched_files)} files "
                f"with unknown intent (task title did not match intent keywords)."
            )

        # ── Nothing generated ─────────────────────────────────────────────
        if not candidates:
            return ReflectionAnalysis(
                worth_retaining=False,
                skip_reason=ReflectionSkipReason.no_new_knowledge,
                retention_reasoning=[
                    "Task completed but no new knowledge pattern detected "
                    "(no constraints, procedures, incidents, or structural changes)."
                ],
            )

        avg_importance = sum(c.importance for c in candidates) / len(candidates)
        avg_confidence = sum(c.confidence for c in candidates) / len(candidates)

        reasoning.insert(0,
            f"Task outcome={inp.task_outcome}, verification={inp.verification_status}, "
            f"intent={intent}: {len(candidates)} candidate(s) generated."
        )

        return ReflectionAnalysis(
            worth_retaining=True,
            retention_reasoning=reasoning,
            suggested_candidates=candidates,
            estimated_importance=round(avg_importance, 3),
            estimated_confidence=round(avg_confidence, 3),
        )

    # ------------------------------------------------------------------
    # Title helpers — produce short, readable candidate titles
    # ------------------------------------------------------------------

    def analyze_and_promote(
        self,
        inp: ReflectionInput,
        project_id_str: str | None = None,
    ) -> tuple[ReflectionAnalysis, list[CandidateCreate]]:
        """Convenience wrapper that returns (analysis, candidates)."""
        analysis = self.analyze(inp)
        return analysis, analysis.suggested_candidates


# ---------------------------------------------------------------------------
# Title generators
# ---------------------------------------------------------------------------


def _source_ref(inp: ReflectionInput) -> str | None:
    files = ", ".join(inp.touched_files[:3])
    return f"post_task_reflection:{files}" if files else "post_task_reflection"


def _constraint_title(text: str) -> str:
    """Turn constraint prose into a short title."""
    # Take first sentence or first 60 chars
    sentence = re.split(r"[.!?]", text.strip())[0]
    return sentence[:80].strip() or text[:60]


def _procedure_title(text: str) -> str:
    sentence = re.split(r"[.!?]", text.strip())[0]
    return sentence[:80].strip() or text[:60]


def _incident_title(inp: ReflectionInput) -> str:
    task = inp.task_description[:60].rstrip()
    return f"Fix: {task}"


def _module_title(inp: ReflectionInput) -> str:
    if inp.module_path:
        return f"{inp.module_path} — updated"
    if inp.touched_files:
        stem = inp.touched_files[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"{stem} — updated"
    return inp.task_description[:60]


def _decision_title(inp: ReflectionInput) -> str:
    return f"Decision: {inp.task_description[:60].rstrip()}"
