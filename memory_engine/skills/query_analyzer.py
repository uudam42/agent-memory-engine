"""QueryAnalyzer — deterministic local analysis of an agent task.

Design principles:
  - No LLM, no external API.  Fully deterministic, fully testable.
  - Same request → same QueryAnalysis, every time.
  - The Protocol interface allows a richer LLM-backed analyzer to be plugged
    in later without changing the RecallService contract.

QueryAnalysis fields:
  task_intent           — classified intent (matches SkillRouter's output)
  relevant_memory_types — which MemoryType buckets are most relevant
  likely_module_paths   — inferred dotted module paths (e.g. "scheduler.retry")
  likely_symbols        — symbol names extracted from task text and current_symbols
  likely_keywords       — significant task keywords for lexical matching
  evidence_expansion_required — True when we should expand raw evidence
  is_high_risk          — True when high-risk keywords detected

DeterministicQueryAnalyzer:
  - Classifies intent using the same ordered regex table as SkillRouter.
  - Extracts module paths by:
      1. Tokenizing current_files into dotted paths (e.g. scheduler/retry.py → scheduler.retry)
      2. Looking for dotted-path patterns in the task text
  - Identifies symbols from current_symbols + CamelCase tokens in task text.
  - Decides evidence expansion based on intent, risk, and task keywords.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from memory_engine.models.domain import MemoryType, TaskIntent

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class QueryAnalysis(BaseModel):
    """Structured analysis of a coding-agent task request."""

    task_intent: TaskIntent
    relevant_memory_types: list[MemoryType]
    likely_module_paths: list[str]         # e.g. ["scheduler.retry", "scheduler.lifecycle"]
    likely_symbols: list[str]              # e.g. ["RetryPolicy", "transition_to_terminal"]
    likely_keywords: list[str]             # significant noun-phrases / terms
    evidence_expansion_required: bool
    is_high_risk: bool


# ---------------------------------------------------------------------------
# Protocol interface (for future LLM-backed implementations)
# ---------------------------------------------------------------------------


@runtime_checkable
class QueryAnalyzerProtocol(Protocol):
    """Interface for task query analyzers.

    Phase 4 uses DeterministicQueryAnalyzer.
    A future LLM-backed analyzer must satisfy this protocol.
    """

    def analyze(
        self,
        task: str,
        current_files: list[str],
        current_symbols: list[str],
    ) -> QueryAnalysis:
        """Analyze an agent task and return a structured QueryAnalysis."""
        ...


# ---------------------------------------------------------------------------
# Deterministic implementation
# ---------------------------------------------------------------------------

# Reuse the same ordered intent patterns as SkillRouter to guarantee agreement
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
    (re.compile(r"\b(document|docs|readme|changelog|spec)\b", re.I), TaskIntent.documentation),
]

_HIGH_RISK_RE = re.compile(
    r"\b(state.?machine|lifecycle|terminal.?state|auth|authen|authori|"
    r"schema|migration|payment|billing|security|credential|secret|token|"
    r"permission|rbac|public.?api|breaking.?change|shared.?infra|"
    r"distributed|consensus|lock|transaction|rollback|idempoten)\b",
    re.I,
)

_EVIDENCE_TRIGGER_RE = re.compile(
    r"\b(incident|bug|deadlock|regression|crash|starvation|infinite loop|"
    r"timeout|failure|broken|flaky|test fail|ci fail|outage)\b",
    re.I,
)

# Intent → memory types (mirrors SkillRouter for consistency)
_INTENT_MEMORY_TYPES: dict[TaskIntent, list[MemoryType]] = {
    TaskIntent.bug_fix: [
        MemoryType.constraint_memory, MemoryType.semantic_memory,
        MemoryType.incident_memory, MemoryType.decision_memory,
    ],
    TaskIntent.test_failure: [
        MemoryType.incident_memory, MemoryType.procedural_memory,
        MemoryType.constraint_memory, MemoryType.semantic_memory,
    ],
    TaskIntent.refactor: [
        MemoryType.constraint_memory, MemoryType.semantic_memory,
        MemoryType.decision_memory, MemoryType.incident_memory,
    ],
    TaskIntent.feature_implementation: [
        MemoryType.semantic_memory, MemoryType.constraint_memory,
        MemoryType.procedural_memory, MemoryType.decision_memory,
    ],
    TaskIntent.architecture_review: [MemoryType.semantic_memory],
    TaskIntent.code_explanation: [MemoryType.semantic_memory, MemoryType.decision_memory],
    TaskIntent.repository_onboarding: [
        MemoryType.semantic_memory, MemoryType.constraint_memory, MemoryType.procedural_memory,
    ],
    TaskIntent.workflow_question: [MemoryType.procedural_memory, MemoryType.semantic_memory],
    TaskIntent.documentation: [MemoryType.semantic_memory],
    TaskIntent.trivial_edit: [],
    TaskIntent.unknown: [MemoryType.semantic_memory],
}

# Stop-words excluded from keyword extraction
_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "have",
    "not", "are", "its", "was", "has", "been", "will", "can",
    "but", "what", "how", "does", "any", "add", "fix", "get",
    "let", "use", "set", "put", "run", "out", "our", "all", "new",
})


def _file_to_module_path(file_path: str) -> str | None:
    """Convert a file path like scheduler/retry.py to scheduler.retry."""
    p = Path(file_path)
    if p.suffix not in (".py", ".ts", ".js", ""):
        return None
    parts = [p.stem] if p.stem != "__init__" else []
    for parent in reversed(p.parents):
        name = parent.name
        if name in ("", ".", "src", "lib", "pkg"):
            continue
        parts.insert(0, name)
    return ".".join(parts) if parts else None


def _extract_camel_symbols(text: str) -> list[str]:
    """Extract CamelCase and snake_case identifiers from free text."""
    # CamelCase with at least 2 upper-case-led parts
    camel = re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", text)
    # snake_case identifiers (at least 2 parts)
    snake = re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text)
    return list(dict.fromkeys(camel + snake))


def _extract_keywords(task: str) -> list[str]:
    """Pull significant noun-phrase tokens from the task description."""
    words = re.findall(r"\b[a-zA-Z][\w]*\b", task)
    keywords: list[str] = []
    for w in words:
        lw = w.lower()
        if lw in _STOP_WORDS or len(lw) <= 2:
            continue
        if lw not in keywords:
            keywords.append(lw)
    return keywords[:20]  # cap at 20


class DeterministicQueryAnalyzer:
    """Fully local, deterministic task analyzer.

    No external calls.  Produces the same QueryAnalysis for the same input.
    """

    def analyze(
        self,
        task: str,
        current_files: list[str],
        current_symbols: list[str],
    ) -> QueryAnalysis:
        intent = self._classify_intent(task)
        is_high_risk = bool(_HIGH_RISK_RE.search(
            task + " " + " ".join(current_files + current_symbols)
        ))

        # Relevant memory types
        mem_types = list(_INTENT_MEMORY_TYPES.get(intent, [MemoryType.semantic_memory]))
        if is_high_risk:
            if MemoryType.constraint_memory not in mem_types:
                mem_types.insert(0, MemoryType.constraint_memory)
            if MemoryType.incident_memory not in mem_types:
                mem_types.append(MemoryType.incident_memory)

        # Module paths from file list + dotted patterns in task text
        module_paths: list[str] = []
        for f in current_files:
            mp = _file_to_module_path(f)
            if mp and mp not in module_paths:
                module_paths.append(mp)
        # Also extract dotted paths from task text (e.g. "scheduler.lifecycle")
        for match in re.finditer(r"\b([a-z_]+\.[a-z_]+(?:\.[a-z_]+)*)\b", task):
            mp = match.group(1)
            if mp not in module_paths:
                module_paths.append(mp)

        # Symbols: explicit list + CamelCase tokens from task
        symbols = list(dict.fromkeys(current_symbols + _extract_camel_symbols(task)))

        # Keywords
        keywords = _extract_keywords(task)

        # Evidence expansion heuristic
        evidence_required = (
            intent in (TaskIntent.bug_fix, TaskIntent.test_failure)
            or is_high_risk
            or bool(_EVIDENCE_TRIGGER_RE.search(task))
        )

        return QueryAnalysis(
            task_intent=intent,
            relevant_memory_types=mem_types,
            likely_module_paths=module_paths,
            likely_symbols=symbols,
            likely_keywords=keywords,
            evidence_expansion_required=evidence_required,
            is_high_risk=is_high_risk,
        )

    def _classify_intent(self, task: str) -> TaskIntent:
        for pattern, intent in _INTENT_PATTERNS:
            if pattern.search(task):
                return intent
        return TaskIntent.unknown
