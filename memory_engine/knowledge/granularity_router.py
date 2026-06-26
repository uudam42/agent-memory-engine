"""GranularityRouter — maps query intent to preferred knowledge retrieval layers.

Phase 10: determines which granularity layer(s) (proposition, paragraph, summary)
to search first, and the expansion policy applied after initial FTS5 hits.

Expansion policies:
  atomic_only       — return only the matched granularity unit, no expansion
  paragraph_expand  — for proposition hits, also fetch the parent paragraph
  summary_overview  — for paragraph/proposition hits, also attach module summary
"""

from __future__ import annotations

from dataclasses import dataclass, field

from memory_engine.models.domain import TaskIntent


@dataclass
class GranularityPreference:
    """Preferred retrieval layers and expansion policy for a query intent."""

    preferred_layers: list[str]        # ordered: most preferred first
    expansion_policy: str              # "atomic_only" | "paragraph_expand" | "summary_overview"
    proposition_types: list[str] | None  # proposition type filter; None = all types
    max_per_layer: int = 10            # max FTS5 hits per layer before merge


# ---------------------------------------------------------------------------
# Intent → granularity preference table
# ---------------------------------------------------------------------------

_INTENT_GRANULARITY: dict[str, GranularityPreference] = {
    # Bug fixes need precise constraint/security/risk propositions, then
    # surrounding paragraph for enough context to apply the fix.
    TaskIntent.bug_fix.value: GranularityPreference(
        preferred_layers=["proposition", "paragraph"],
        expansion_policy="paragraph_expand",
        proposition_types=["constraint", "security_rule", "risk"],
    ),
    # Test failures: evidence and risk propositions are most useful.
    TaskIntent.test_failure.value: GranularityPreference(
        preferred_layers=["proposition", "paragraph"],
        expansion_policy="paragraph_expand",
        proposition_types=["test_evidence", "risk", "constraint"],
    ),
    # Refactors: architectural context at paragraph + decision propositions.
    TaskIntent.refactor.value: GranularityPreference(
        preferred_layers=["paragraph", "summary"],
        expansion_policy="paragraph_expand",
        proposition_types=["architecture", "decision", "constraint"],
    ),
    # Feature work: broad scan — paragraphs first for interface shapes, then
    # module summaries for dependency overview.
    TaskIntent.feature_implementation.value: GranularityPreference(
        preferred_layers=["paragraph", "proposition", "summary"],
        expansion_policy="paragraph_expand",
        proposition_types=None,
    ),
    # Architecture reviews: summaries give the fastest module-level overview.
    TaskIntent.architecture_review.value: GranularityPreference(
        preferred_layers=["summary", "paragraph"],
        expansion_policy="summary_overview",
        proposition_types=["architecture", "decision"],
    ),
    # Code explanation: paragraphs carry narrative structure; propositions
    # surface key facts.
    TaskIntent.code_explanation.value: GranularityPreference(
        preferred_layers=["paragraph", "proposition"],
        expansion_policy="paragraph_expand",
        proposition_types=None,
    ),
    # Onboarding: summaries give the broadest module map quickly.
    TaskIntent.repository_onboarding.value: GranularityPreference(
        preferred_layers=["summary", "paragraph"],
        expansion_policy="summary_overview",
        proposition_types=["architecture", "constraint"],
    ),
    # Workflow questions: procedure propositions + paragraphs for steps.
    TaskIntent.workflow_question.value: GranularityPreference(
        preferred_layers=["proposition", "paragraph"],
        expansion_policy="paragraph_expand",
        proposition_types=["procedure", "behavior"],
    ),
    # Documentation tasks: paragraphs for prose context.
    TaskIntent.documentation.value: GranularityPreference(
        preferred_layers=["paragraph", "summary"],
        expansion_policy="paragraph_expand",
        proposition_types=None,
    ),
    # Trivial edits don't need deep context retrieval.
    TaskIntent.trivial_edit.value: GranularityPreference(
        preferred_layers=["paragraph"],
        expansion_policy="atomic_only",
        proposition_types=None,
    ),
    # Unknown intent: broad multi-layer search.
    TaskIntent.unknown.value: GranularityPreference(
        preferred_layers=["proposition", "paragraph", "summary"],
        expansion_policy="paragraph_expand",
        proposition_types=None,
    ),
}

_DEFAULT_PREF = GranularityPreference(
    preferred_layers=["paragraph", "proposition"],
    expansion_policy="paragraph_expand",
    proposition_types=None,
)


class GranularityRouter:
    """Routes a query intent to the optimal knowledge granularity layer(s)."""

    def route(self, intent: str | TaskIntent) -> GranularityPreference:
        """Return the GranularityPreference for a given TaskIntent."""
        key = intent.value if isinstance(intent, TaskIntent) else intent
        return _INTENT_GRANULARITY.get(key, _DEFAULT_PREF)
