"""SkillRouter — determines whether the agent needs memory recall before working.

Design principles:
- Fully deterministic. Same request → same RoutingPlan, every time.
- No LLM calls. Decision rules are explicit and unit-testable.
- Minimal. Retrieves only what the task actually needs.
- Respects explicit user instructions (no_memory, isolated).

Intent classification uses ordered keyword matching.  The first matching
intent wins, so more-specific patterns are listed before broader ones.

Complexity derives from intent + task length + file/symbol counts.

Risk derives from presence of high-risk keywords in the task description
and in the touched files.  A risk_hint from the caller overrides computed
risk only upward (never silently downgrade user-supplied risk).
"""

from __future__ import annotations

import re

from memory_engine.models.domain import (
    MemoryType,
    RiskLevel,
    RoutingPlan,
    RouteRequest,
    TaskComplexity,
    TaskIntent,
)

# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

# (pattern, intent)  — evaluated in order; first match wins
_INTENT_PATTERNS: list[tuple[re.Pattern[str], TaskIntent]] = [
    (re.compile(r"\b(test fail|flaky test|assertion error|ci fail|test break)\b", re.I), TaskIntent.test_failure),
    (re.compile(r"\b(rename|format|whitespace|typo|comment|docstring|lint)\b", re.I), TaskIntent.trivial_edit),
    (re.compile(r"\b(bug|fix|regression|crash|error|exception|broken|incorrect)\b", re.I), TaskIntent.bug_fix),
    (re.compile(r"\b(refactor|restructure|reorganize|clean up|extract|split|decouple)\b", re.I), TaskIntent.refactor),
    (re.compile(r"\b(add|implement|create|build|introduce|support|enable)\b", re.I), TaskIntent.feature_implementation),
    (re.compile(r"\b(onboard|onboarding|getting started|orient)\b", re.I), TaskIntent.repository_onboarding),
    (re.compile(r"\b(architecture|design|system|subsystem|overview|diagram)\b", re.I), TaskIntent.architecture_review),
    (re.compile(r"\b(explain|understand|what does|how does|describe|walk me)\b", re.I), TaskIntent.code_explanation),
    (re.compile(r"\b(workflow|process|pipeline|procedure|how to)\b", re.I), TaskIntent.workflow_question),
    (re.compile(r"\b(document|docs|readme|changelog|spec|changelog)\b", re.I), TaskIntent.documentation),
]

# ---------------------------------------------------------------------------
# High-risk keyword patterns — touching these elevates risk
# ---------------------------------------------------------------------------

_HIGH_RISK_PATTERNS = re.compile(
    r"\b(state.?machine|lifecycle|terminal.?state|auth|authen|authori|"
    r"schema|migration|payment|billing|security|credential|secret|token|"
    r"permission|rbac|public.?api|breaking.?change|shared.?infra|"
    r"distributed|consensus|lock|transaction|rollback|idempoten)\b",
    re.I,
)

_MEDIUM_RISK_PATTERNS = re.compile(
    r"\b(retry|timeout|backoff|queue|worker|scheduler|cron|job|"
    r"config|env|setting|flag|feature.?flag|cache|session|cookie)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Intent → required memory types
# ---------------------------------------------------------------------------

_INTENT_MEMORY_TYPES: dict[TaskIntent, list[MemoryType]] = {
    TaskIntent.bug_fix: [
        MemoryType.constraint_memory,
        MemoryType.semantic_memory,
        MemoryType.incident_memory,
        MemoryType.decision_memory,
    ],
    TaskIntent.test_failure: [
        MemoryType.incident_memory,
        MemoryType.procedural_memory,
        MemoryType.constraint_memory,
        MemoryType.semantic_memory,
    ],
    TaskIntent.refactor: [
        MemoryType.constraint_memory,
        MemoryType.semantic_memory,
        MemoryType.decision_memory,
        MemoryType.incident_memory,
    ],
    TaskIntent.feature_implementation: [
        MemoryType.semantic_memory,
        MemoryType.constraint_memory,
        MemoryType.procedural_memory,
        MemoryType.decision_memory,
    ],
    TaskIntent.architecture_review: [
        MemoryType.semantic_memory,
    ],
    TaskIntent.code_explanation: [
        MemoryType.semantic_memory,
        MemoryType.decision_memory,
    ],
    TaskIntent.repository_onboarding: [
        MemoryType.semantic_memory,
        MemoryType.constraint_memory,
        MemoryType.procedural_memory,
    ],
    TaskIntent.workflow_question: [
        MemoryType.procedural_memory,
        MemoryType.semantic_memory,
    ],
    TaskIntent.documentation: [
        MemoryType.semantic_memory,
    ],
    TaskIntent.trivial_edit: [],  # skip unless elevated
    TaskIntent.unknown: [MemoryType.semantic_memory],
}

# ---------------------------------------------------------------------------
# Intent → token budget
# ---------------------------------------------------------------------------

_INTENT_TOKEN_BUDGET: dict[TaskIntent, int] = {
    TaskIntent.bug_fix: 5500,
    TaskIntent.test_failure: 5000,
    TaskIntent.refactor: 6000,
    TaskIntent.feature_implementation: 5000,
    TaskIntent.architecture_review: 4000,
    TaskIntent.code_explanation: 3000,
    TaskIntent.repository_onboarding: 4500,
    TaskIntent.workflow_question: 2500,
    TaskIntent.documentation: 2000,
    TaskIntent.trivial_edit: 0,
    TaskIntent.unknown: 3000,
}

# ---------------------------------------------------------------------------
# No-memory flags
# ---------------------------------------------------------------------------

_NO_MEMORY_FLAGS: frozenset[str] = frozenset(
    {"no_memory", "no-memory", "isolated", "do_not_use_memory", "do_not_persist"}
)


# ---------------------------------------------------------------------------
# SkillRouter
# ---------------------------------------------------------------------------


class SkillRouter:
    """Classifies a task and decides whether memory recall is warranted."""

    def route(self, request: RouteRequest) -> RoutingPlan:
        """Produce a deterministic routing plan for the given task request."""

        task = request.current_task
        flags = {f.lower() for f in request.user_instruction_flags}

        # ----- Check explicit no-memory instruction -----
        if flags & _NO_MEMORY_FLAGS:
            return RoutingPlan(
                should_recall_memory=False,
                should_allow_deep_inspection=False,
                task_intent=self._classify_intent(task),
                task_complexity=TaskComplexity.trivial,
                risk_level=RiskLevel.low,
                required_memory_types=[],
                recommended_token_budget=0,
                reasoning=["User explicitly requested no memory recall."],
                persistence_allowed=False,
            )

        intent = self._classify_intent(task)
        complexity = self._classify_complexity(intent, task, request)
        risk = self._classify_risk(task, request, intent)

        # Apply risk_hint — only elevate, never silently lower
        if request.risk_hint is not None:
            risk = self._max_risk(risk, request.risk_hint)

        should_recall = self._should_recall(intent, complexity, risk, task)
        should_deep = self._should_deep_inspect(intent, risk)
        memory_types = self._required_memory_types(intent, risk)
        budget = _INTENT_TOKEN_BUDGET.get(intent, 3000) if should_recall else 0
        reasoning = self._build_reasoning(intent, complexity, risk, should_recall, task)
        persistence_allowed = "no_memory" not in flags and "isolated" not in flags

        return RoutingPlan(
            should_recall_memory=should_recall,
            should_allow_deep_inspection=should_deep,
            task_intent=intent,
            task_complexity=complexity,
            risk_level=risk,
            required_memory_types=memory_types,
            recommended_token_budget=budget,
            reasoning=reasoning,
            persistence_allowed=persistence_allowed,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _classify_intent(self, task: str) -> TaskIntent:
        for pattern, intent in _INTENT_PATTERNS:
            if pattern.search(task):
                return intent
        return TaskIntent.unknown

    def _classify_complexity(
        self, intent: TaskIntent, task: str, request: RouteRequest
    ) -> TaskComplexity:
        if intent == TaskIntent.trivial_edit:
            return TaskComplexity.trivial
        if intent in (TaskIntent.documentation, TaskIntent.workflow_question):
            return TaskComplexity.low

        word_count = len(task.split())
        file_count = len(request.current_files)
        symbol_count = len(request.current_symbols)

        score = 0
        if word_count > 15:
            score += 1
        if word_count > 30:
            score += 1
        if file_count > 2:
            score += 1
        if symbol_count > 3:
            score += 1
        if intent in (TaskIntent.refactor, TaskIntent.feature_implementation):
            score += 1
        if intent in (TaskIntent.bug_fix, TaskIntent.test_failure):
            score += 1

        if score == 0:
            return TaskComplexity.low
        if score <= 2:
            return TaskComplexity.medium
        return TaskComplexity.high

    def _classify_risk(
        self, task: str, request: RouteRequest, intent: TaskIntent
    ) -> RiskLevel:
        combined = task + " " + " ".join(request.current_files + request.current_symbols)

        if _HIGH_RISK_PATTERNS.search(combined):
            return RiskLevel.high
        if _MEDIUM_RISK_PATTERNS.search(combined):
            return RiskLevel.medium
        if intent in (TaskIntent.refactor, TaskIntent.feature_implementation, TaskIntent.bug_fix):
            return RiskLevel.medium
        return RiskLevel.low

    def _max_risk(self, computed: RiskLevel, hint: RiskLevel) -> RiskLevel:
        order = [RiskLevel.low, RiskLevel.medium, RiskLevel.high]
        return order[max(order.index(computed), order.index(hint))]

    def _should_recall(
        self,
        intent: TaskIntent,
        complexity: TaskComplexity,
        risk: RiskLevel,
        task: str,
    ) -> bool:
        # Trivial edits — skip unless risk is elevated
        if intent == TaskIntent.trivial_edit:
            return risk in (RiskLevel.medium, RiskLevel.high)
        # Documentation — typically no recall needed
        if intent == TaskIntent.documentation and risk == RiskLevel.low:
            return False
        # Everything else with non-trivial complexity → recall
        return complexity != TaskComplexity.trivial

    def _should_deep_inspect(self, intent: TaskIntent, risk: RiskLevel) -> bool:
        if risk == RiskLevel.high:
            return True
        if intent in (TaskIntent.bug_fix, TaskIntent.test_failure, TaskIntent.refactor):
            return True
        return False

    def _required_memory_types(
        self, intent: TaskIntent, risk: RiskLevel
    ) -> list[MemoryType]:
        base = list(_INTENT_MEMORY_TYPES.get(intent, []))
        # High-risk tasks always need constraint memory and incident memory
        if risk == RiskLevel.high:
            if MemoryType.constraint_memory not in base:
                base.insert(0, MemoryType.constraint_memory)
            if MemoryType.incident_memory not in base:
                base.append(MemoryType.incident_memory)
        return base

    def _build_reasoning(
        self,
        intent: TaskIntent,
        complexity: TaskComplexity,
        risk: RiskLevel,
        should_recall: bool,
        task: str,
    ) -> list[str]:
        reasons: list[str] = []
        reasons.append(f"Task classified as intent={intent.value}, complexity={complexity.value}, risk={risk.value}.")

        if not should_recall:
            reasons.append("Memory recall skipped: task is trivial or explicitly excluded.")
            return reasons

        if intent == TaskIntent.bug_fix:
            reasons.append("Bug fix tasks recall relevant module knowledge, incidents, and prior decisions.")
        elif intent == TaskIntent.test_failure:
            reasons.append("Test failure tasks recall incidents and procedures.")
        elif intent == TaskIntent.refactor:
            reasons.append("Refactor tasks recall constraints, architecture, and historical decisions.")
        elif intent == TaskIntent.feature_implementation:
            reasons.append("Feature tasks recall module context, constraints, and procedures.")
        elif intent in (TaskIntent.architecture_review, TaskIntent.code_explanation):
            reasons.append("Explanation tasks recall project and subsystem summaries.")
        elif intent == TaskIntent.repository_onboarding:
            reasons.append("Onboarding tasks recall the full project knowledge base.")

        if risk == RiskLevel.high:
            reasons.append("High-risk signals detected — constraints and incidents will be prioritised.")
        if _HIGH_RISK_PATTERNS.search(task):
            reasons.append("Task description mentions high-risk concepts (state machine, auth, schema, etc.).")

        return reasons
