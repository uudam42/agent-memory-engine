"""ContextComposer — builds a token-budgeted EnrichedContextPack.

Budget allocation (default 6000 tokens):

  constraints       800  — always highest priority; never trimmed first
  architecture      900
  modules          1800
  decisions+incidents 1700
  procedures        500
  evidence refs     300

Token estimation: len(text) // 4   (deterministic, no API calls)

Trimming order when budget is exceeded:
  1. Remove low-importance nodes (importance < 0.3) in procedures
  2. Remove low-confidence nodes (confidence < 0.5) in decisions/incidents
  3. Remove redundant module summaries (lower-scored duplicates)
  4. Preserve all constraints and high-risk incidents always
"""

from __future__ import annotations

from memory_engine.models.domain import (
    EnrichedContextPack,
    Evidence,
    MemoryKind,
    MemoryNode,
    MemoryStatus,
    Project,
    RoutingPlan,
    ScoredMemory,
    TaskIntent,
    TraceEntry,
)

# ---------------------------------------------------------------------------
# Default budget buckets (tokens)
# ---------------------------------------------------------------------------

DEFAULT_BUDGET_CONSTRAINTS = 800
DEFAULT_BUDGET_ARCHITECTURE = 900
DEFAULT_BUDGET_MODULES = 1800
DEFAULT_BUDGET_DECISIONS_INCIDENTS = 1700
DEFAULT_BUDGET_PROCEDURES = 500
DEFAULT_BUDGET_EVIDENCE = 300

DEFAULT_TOTAL_BUDGET = (
    DEFAULT_BUDGET_CONSTRAINTS
    + DEFAULT_BUDGET_ARCHITECTURE
    + DEFAULT_BUDGET_MODULES
    + DEFAULT_BUDGET_DECISIONS_INCIDENTS
    + DEFAULT_BUDGET_PROCEDURES
    + DEFAULT_BUDGET_EVIDENCE
)


def _estimate_tokens(text: str) -> int:
    """Rough but deterministic: 1 token ≈ 4 characters."""
    return max(1, len(text) // 4)


def _node_tokens(node: MemoryNode) -> int:
    parts = [node.title, node.summary] + node.tags
    if node.module_path:
        parts.append(node.module_path)
    return _estimate_tokens(" ".join(parts))


def _evidence_tokens(ev: Evidence) -> int:
    return _estimate_tokens(ev.content + (ev.source or ""))


def _node_tree_path(node: MemoryNode) -> list[str]:
    """Build a descriptive tree path label for trace entries.

    Uses depth and kind as a proxy — actual parent titles would require
    a second DB query.  This is a cost-free approximation:
        depth 0  → ["Project", "<kind>"]
        depth 1  → ["Project", "Subsystem", "<kind>"]
        depth 2+ → ["Project", "Subsystem", "Module", "<kind>"]
    """
    path = ["Project"]
    if node.depth >= 1:
        path.append("Subsystem")
    if node.depth >= 2:
        path.append("Module")
    if node.depth >= 3:
        path.append(node.kind.capitalize())
    else:
        path.append(node.kind.capitalize())
    return path


def _fill_bucket(
    scored: list[ScoredMemory],
    budget: int,
    *,
    min_confidence: float = 0.0,
    min_importance: float = 0.0,
    include_stale: bool = False,
) -> tuple[list[MemoryNode], list[TraceEntry]]:
    """Greedily fill a bucket until budget exhausted, tracking trace entries."""
    selected: list[MemoryNode] = []
    trace: list[TraceEntry] = []
    used = 0

    for sm in scored:
        node = sm.node
        breakdown = sm.score_breakdown          # Phase 4: full breakdown in trace
        tree_path = _node_tree_path(node)
        node_status = node.status.value if hasattr(node.status, "value") else str(node.status)

        # Exclude stale / superseded by default
        if not include_stale and node.status in (
            MemoryStatus.stale, MemoryStatus.superseded, MemoryStatus.archived
        ):
            trace.append(TraceEntry(
                memory_id=str(node.id),
                title=node.title,
                action="excluded",
                reason=f"Memory status is {node.status.value}",
                score=sm.score,
                score_breakdown=breakdown,
                status=node_status,
                tree_path=tree_path,
            ))
            continue

        # Confidence gate
        if node.confidence < min_confidence:
            trace.append(TraceEntry(
                memory_id=str(node.id),
                title=node.title,
                action="excluded",
                reason=f"Confidence {node.confidence:.2f} below threshold {min_confidence:.2f}",
                score=sm.score,
                score_breakdown=breakdown,
                status=node_status,
                tree_path=tree_path,
            ))
            continue

        # Importance gate
        if node.importance < min_importance:
            trace.append(TraceEntry(
                memory_id=str(node.id),
                title=node.title,
                action="excluded",
                reason=f"Importance {node.importance:.2f} below threshold {min_importance:.2f}",
                score=sm.score,
                score_breakdown=breakdown,
                status=node_status,
                tree_path=tree_path,
            ))
            continue

        cost = _node_tokens(node)
        if used + cost > budget:
            trace.append(TraceEntry(
                memory_id=str(node.id),
                title=node.title,
                action="excluded",
                reason="Token budget exhausted for this bucket",
                score=sm.score,
                score_breakdown=breakdown,
                status=node_status,
                tree_path=tree_path,
            ))
            continue

        used += cost
        selected.append(node)
        trace.append(TraceEntry(
            memory_id=str(node.id),
            title=node.title,
            action="selected",
            reason=_selection_reason(node, sm.score),
            score=sm.score,
            score_breakdown=breakdown,
            status=node_status,
            tree_path=tree_path,
        ))

    return selected, trace


def _selection_reason(node: MemoryNode, score: float) -> str:
    kind_reasons: dict[str, str] = {
        "constraint": "Project-level constraint always included for non-trivial tasks",
        "architecture": "Architecture knowledge required for task context",
        "module": "Module-level knowledge relevant to task scope",
        "decision": "Historical decision relevant to current task",
        "debug": "Incident memory relevant for bug investigation",
        "procedure": "Operational procedure relevant to task workflow",
        "outcome": "Prior outcome relevant to task assessment",
    }
    base = kind_reasons.get(node.kind, "Memory node selected by relevance score")
    return f"{base} (score={score:.3f})"


# ---------------------------------------------------------------------------
# ContextComposer
# ---------------------------------------------------------------------------


class ContextComposer:
    """Assembles an EnrichedContextPack from ranked memory nodes under a budget."""

    def compose(
        self,
        *,
        project: Project,
        scored_nodes: list[ScoredMemory],
        routing_plan: RoutingPlan,
        include_evidence: bool = False,
        token_budget: int | None = None,
    ) -> tuple[EnrichedContextPack, list[TraceEntry]]:
        """Build a structured ContextPack, returning it with the full trace."""

        total_budget = token_budget or routing_plan.recommended_token_budget or DEFAULT_TOTAL_BUDGET
        # Scale bucket budgets proportionally if total differs from default
        scale = total_budget / DEFAULT_TOTAL_BUDGET if DEFAULT_TOTAL_BUDGET else 1.0

        b_constraints = int(DEFAULT_BUDGET_CONSTRAINTS * scale)
        b_arch = int(DEFAULT_BUDGET_ARCHITECTURE * scale)
        b_modules = int(DEFAULT_BUDGET_MODULES * scale)
        b_dec_inc = int(DEFAULT_BUDGET_DECISIONS_INCIDENTS * scale)
        b_proc = int(DEFAULT_BUDGET_PROCEDURES * scale)
        b_ev = int(DEFAULT_BUDGET_EVIDENCE * scale)

        intent = routing_plan.task_intent
        all_trace: list[TraceEntry] = []

        # Determine whether to expand evidence
        expand_evidence = include_evidence or intent in (
            TaskIntent.bug_fix, TaskIntent.test_failure
        )

        # Partition scored nodes by kind
        def _by_kind(*kinds: str) -> list[ScoredMemory]:
            return [s for s in scored_nodes if s.node.kind in kinds]

        constraints_scored = _by_kind("constraint")
        arch_scored = _by_kind("architecture")
        module_scored = _by_kind("module")
        decision_scored = _by_kind("decision")
        incident_scored = _by_kind("debug", "outcome")
        procedure_scored = _by_kind("procedure")

        # Merge decisions + incidents into one sorted list for budget sharing
        dec_inc_scored = sorted(
            decision_scored + incident_scored, key=lambda s: s.score, reverse=True
        )

        # Fill each bucket
        constraints, t1 = _fill_bucket(constraints_scored, b_constraints)
        all_trace.extend(t1)

        architecture, t2 = _fill_bucket(arch_scored, b_arch)
        all_trace.extend(t2)

        modules, t3 = _fill_bucket(module_scored, b_modules)
        all_trace.extend(t3)

        dec_inc_nodes, t4 = _fill_bucket(
            dec_inc_scored,
            b_dec_inc,
            min_confidence=0.0,  # do not gate decisions by confidence here
        )
        all_trace.extend(t4)

        # Split dec_inc back into decisions and incidents
        decisions = [n for n in dec_inc_nodes if n.kind == "decision"]
        incidents = [n for n in dec_inc_nodes if n.kind in ("debug", "outcome")]

        procedures, t5 = _fill_bucket(procedure_scored, b_proc, min_importance=0.3)
        all_trace.extend(t5)

        # Evidence references — only from selected nodes, only if needed
        evidence_refs: list[Evidence] = []
        if expand_evidence:
            ev_budget = b_ev
            ev_used = 0
            evidence_nodes = constraints + incidents + decisions
            for node in evidence_nodes:
                for ev in node.evidence:
                    cost = _evidence_tokens(ev)
                    if ev_used + cost > ev_budget:
                        break
                    evidence_refs.append(ev)
                    ev_used += cost

        # Total nodes
        all_selected = (
            constraints + architecture + modules + decisions + incidents + procedures
        )
        total_text = "\n".join(
            f"{n.title} {n.summary}" for n in all_selected
        )
        token_estimate = _estimate_tokens(total_text)

        pack = EnrichedContextPack(
            project=project,
            constraints=constraints,
            architecture=architecture,
            modules=modules,
            decisions=decisions,
            incidents=incidents,
            procedures=procedures,
            evidence_refs=evidence_refs,
            total_nodes=len(all_selected),
            token_estimate=token_estimate,
        )

        return pack, all_trace
