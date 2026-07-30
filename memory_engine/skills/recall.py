"""RecallService — autonomous hierarchical memory retrieval.

Phase 4 retrieval flow:
  1. Run DeterministicQueryAnalyzer on the task to extract structured signals.
  2. If no routing_plan supplied → call SkillRouter (uses same signals).
  3. If routing says skip → return empty RecallResult with recall_skipped=True.
  4. Load ALL project nodes (status filtering happens in composer so the
     trace can explain why stale/superseded nodes were excluded).
  5. Score all nodes with DeterministicRanker (9 signals including
     semantic_similarity placeholder).
  6. ContextComposer fills typed buckets under the recommended token budget,
     generating a full retrieval trace with score_breakdown, status, tree_path.
  7. Return RecallResult with context_pack, routing_plan, retrieval_trace.

Never calls an external API or LLM.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    EnrichedContextPack,
    MemoryNode,
    Project,
    RecallRequest,
    RecallResult,
    RoutingPlan,
    ScoredMemory,
    TaskIntent,
    TraceEntry,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.repositories.project import ProjectRepository
from memory_engine.repositories.relation import RelationRepository
from memory_engine.services.constraint_scope import constraint_is_eligible
from memory_engine.services.conflict_detection import detect_conflicts
from memory_engine.services.memory_service import ProjectNotFoundError
from memory_engine.services.source_validity import SourceValidityService
from memory_engine.skills.composer import ContextComposer, build_provenance
from memory_engine.skills.query_analyzer import DeterministicQueryAnalyzer, QueryAnalyzerProtocol
from memory_engine.skills.ranker import DeterministicRanker
from memory_engine.skills.router import SkillRouter

# Words that carry no topic signal and would cause false Jaccard matches.
_GATE_STOP_WORDS = frozenset({
    "use", "used", "uses", "using", "for", "the", "and", "with", "all",
    "must", "can", "are", "not", "per", "its", "our", "how", "should",
    "what", "this", "that", "from", "have", "has", "was", "will", "been",
    "into", "any", "but", "via", "such", "each", "also", "may", "new",
    "get", "set", "let", "only", "than", "over", "when", "then", "here",
    "more", "some", "very", "just", "like", "make", "take", "need",
})

# Minimum character length for a word to be considered a topic signal.
_GATE_MIN_WORD_LEN = 4

# Phase 3A review (A8): bound the number of source-backed nodes validated in
# a single recall() call. Without this cap, a project with N source-backed
# memories performs up to N file reads (existence + hash + optional
# symbol/commit checks) on every cache-miss recall, regardless of the
# requested token budget or how many of those memories would ever be
# selected into the response — an unbounded-I/O pattern proportional to
# total project memory count, not to the request. Nodes beyond the cap are
# left unchecked for this call (no status change, no I/O) rather than
# either scanning unconditionally or blocking retrieval; a later call (or
# explicit revalidation) will still catch a status transition it missed.
# This is a conservative minimal bound, not a full indexing subsystem.
_MAX_VALIDITY_CHECKS_PER_RECALL = 50


def _gate_words(text: str) -> frozenset[str]:
    """Extract meaningful words (length >= 4, not stop words) for gate check."""
    return frozenset(
        w for w in re.findall(r"\w+", text.lower())
        if len(w) >= _GATE_MIN_WORD_LEN and w not in _GATE_STOP_WORDS
    )


def _passes_relevance_gate(
    task: str,
    scored: ScoredMemory,
    current_files: list[str] | None = None,
    current_branch: str | None = None,
    task_intent: str | None = None,
) -> bool:
    """Return True if the node has at least one topic signal for the task.

    Gate applies to authoritative memory kinds (architecture, decision) to
    prevent high-importance/freshness scores from surfacing completely
    off-domain infrastructure memories.

    A node passes when ANY of the following is true:
    - Strong structural signal: module_path_overlap > 0 or symbol_overlap > 0
    - Strong semantic signal: semantic_similarity >= 0.4 (when available)
    - Lexical overlap: task_words ∩ node_words is non-empty
    - Task is too short for meaningful gating (all stop-words or < 4 chars per word)

    Gated kinds (applied when none of the above holds):
    - architecture: always gated (original behavior)
    - decision: gated — but only when both lexical AND structural signals are zero

    constraint: NOT handled by this generic gate at all (Issue 2). Instead,
    a constraint bypasses the topical gate only when its explicit or
    inferred ConstraintScope makes it eligible for the current request (see
    constraint_scope.constraint_is_eligible) — an unscoped/off-topic
    constraint no longer surfaces on every task merely because its kind is
    "constraint". See _passes_constraint_gate below.

    Always passes through (composer handles these via importance/status):
    - procedure, debug (incident), module, outcome
    """
    from memory_engine.models.domain import MemoryKind

    node = scored.node

    if node.kind == MemoryKind.constraint:
        return _passes_constraint_gate(scored, current_files, current_branch, task_intent)

    # Gate authoritative kinds that tend to be high-importance and domain-spanning.
    _GATED_KINDS = frozenset({
        MemoryKind.architecture,
        MemoryKind.decision,
    })
    if node.kind not in _GATED_KINDS:
        return True

    bd = scored.score_breakdown

    # Structural signal: file or symbol overlap → strong relevance evidence
    if bd.get("module_path_overlap", 0.0) > 0.0 or bd.get("symbol_overlap", 0.0) > 0.0:
        return True

    # Semantic signal: high similarity → relevant even without lexical overlap
    # (handles synonym / multilingual cases)
    if bd.get("semantic_similarity", 0.0) >= 0.4:
        return True

    task_words = _gate_words(task)
    if not task_words:
        return True  # too short to gate meaningfully

    node_text = f"{node.title} {node.summary} {' '.join(node.tags)}"
    node_words = _gate_words(node_text)

    return bool(task_words & node_words)


def _passes_constraint_gate(
    scored: ScoredMemory,
    current_files: list[str] | None,
    current_branch: str | None,
    task_intent: str | None,
) -> bool:
    """Issue 2 — scope-aware constraint eligibility.

    Replaces the old "constraint always bypasses the relevance gate"
    behavior. A constraint now surfaces unconditionally only when its scope
    makes it eligible for the current request; otherwise it is subject to
    the same topical gate as any other memory kind (falls through below).
    """
    bd = scored.score_breakdown
    eligible = constraint_is_eligible(
        scored.node,
        module_path_overlap=bd.get("module_path_overlap", 0.0),
        symbol_overlap=bd.get("symbol_overlap", 0.0),
        current_files=current_files,
        current_branch=current_branch,
        task_intent=task_intent,
    )
    return eligible


class RecallService:
    def __init__(
        self,
        session: Session,
        query_analyzer: QueryAnalyzerProtocol | None = None,
        project_root: str | Path | None = None,
        validity_service: SourceValidityService | None = None,
        revision_hook: Callable[[], None] | None = None,
    ) -> None:
        self._nodes = MemoryNodeRepository(session)
        self._projects = ProjectRepository(session)
        self._relations = RelationRepository(session)
        self._router = SkillRouter()
        self._ranker = DeterministicRanker()
        self._composer = ContextComposer()
        # Phase 4: inject analyzer (defaults to deterministic local fallback)
        self._query_analyzer: QueryAnalyzerProtocol = (
            query_analyzer or DeterministicQueryAnalyzer()
        )
        # Phase 15 (Issue 1): when project_root is supplied, source-backed
        # candidates are lazily validated before ranking. Defaults to None,
        # which fully preserves pre-Phase-15 behavior (no filesystem access,
        # no validity transitions) for every caller that doesn't opt in.
        self._project_root: Path | None = Path(project_root) if project_root else None
        self._validity: SourceValidityService = validity_service or SourceValidityService()
        # Task 5: optional callback fired exactly once per persisted validity
        # transition (never on a no-op check), so the caller can bump a
        # generation/revision counter that participates in cache keys.
        self._revision_hook: Callable[[], None] | None = revision_hook

    def recall(self, request: RecallRequest) -> RecallResult:
        """Primary entry point — autonomous memory recall for an agent task.

        Phase 4 flow:
          1. QueryAnalyzer extracts structured signals from the task text.
          2. SkillRouter uses those signals to decide whether recall is needed.
          3. DeterministicRanker scores all nodes (9 signals).
          4. ContextComposer assembles token-budgeted ContextPack with trace.
        """

        # -- Phase 4: Analyze task query ----------------------------------
        query_analysis = self._query_analyzer.analyze(
            request.current_task,
            request.current_files,
            request.current_symbols,
        )

        # -- Resolve routing plan -----------------------------------------
        from memory_engine.models.domain import RouteRequest

        routing_plan: RoutingPlan
        if request.routing_plan is not None:
            routing_plan = request.routing_plan
        else:
            route_req = RouteRequest(
                project_id=request.project_id,
                current_task=request.current_task,
                current_files=request.current_files,
                current_symbols=request.current_symbols,
            )
            routing_plan = self._router.route(route_req)

        # -- Short-circuit when skip is indicated -------------------------
        if not routing_plan.should_recall_memory:
            project = self._load_project(str(request.project_id))
            empty_pack = EnrichedContextPack(
                project=Project.model_validate(project),
                total_nodes=0,
                token_estimate=0,
            )
            return RecallResult(
                context_pack=empty_pack,
                routing_plan=routing_plan,
                retrieval_trace=[],
                token_estimate=0,
                recall_skipped=True,
                skip_reason=routing_plan.reasoning[0] if routing_plan.reasoning else "Recall not required.",
            )

        # -- Load project --------------------------------------------------
        project_orm = self._load_project(str(request.project_id))
        project = Project.model_validate(project_orm)

        # -- Load all nodes — status filtering happens in composer ---------
        # Loading all statuses ensures the retrieval trace can explain why
        # stale / superseded nodes were excluded (not silently dropped at DB level).
        # The composer's _fill_bucket() gates on status per bucket.
        orm_nodes = self._nodes.list_by_project(str(request.project_id))
        nodes: list[MemoryNode] = [MemoryNode.model_validate(o) for o in orm_nodes]

        # -- Phase 15 (Issue 1): lazy source-validity check ------------------
        # Only runs when a project_root was supplied at construction time, and
        # only touches nodes that carry explicit source_path evidence — bounded
        # to the already-loaded candidate list, no repository-wide scan.
        # Transitions are persisted (auditable) and reflected in-memory so the
        # same recall call excludes newly-invalidated nodes immediately.
        if self._project_root is not None:
            hash_cache: dict[str, str | None] = {}
            checks_done = 0
            for node in nodes:
                if not node.source_path:
                    continue
                if checks_done >= _MAX_VALIDITY_CHECKS_PER_RECALL:
                    break
                checks_done += 1
                result = self._validity.check(
                    node, self._project_root, hash_cache=hash_cache
                )
                if not result.changed:
                    continue
                updated = self._nodes.set_validity(
                    str(node.id),
                    new_status=result.new_status.value,
                    reason=result.reason or "",
                )
                if updated is not None:
                    node.status = result.new_status
                    node.previous_status = MemoryNode.model_validate(updated).previous_status
                    node.validity_reason = result.reason
                    if self._revision_hook is not None:
                        self._revision_hook()

        # -- Score all nodes (use QueryAnalysis to enrich file/symbol signals) --
        # Merge: explicit request signals + QueryAnalyzer inferences
        enriched_files = list(dict.fromkeys(
            request.current_files + query_analysis.likely_module_paths
        ))
        enriched_symbols = list(dict.fromkeys(
            request.current_symbols + query_analysis.likely_symbols
        ))

        scored = self._ranker.rank(
            nodes,
            task=request.current_task,
            intent=routing_plan.task_intent,
            current_files=enriched_files,
            current_symbols=enriched_symbols,
            current_branch=request.current_branch,
        )

        # -- Compose context pack under budget ----------------------------
        budget = request.token_budget or routing_plan.recommended_token_budget
        expand_evidence = (
            query_analysis.evidence_expansion_required
            or routing_plan.task_intent in (TaskIntent.bug_fix, TaskIntent.test_failure)
        )

        # -- Relevance gate: remove nodes with no topic signal ----------------
        # Nodes that share no meaningful words with the task (after stop-word
        # filtering) and have no file/symbol structural overlap are excluded
        # before the composer. Without this gate, importance+freshness+confidence
        # can surface high-quality but completely off-topic memories.
        task = request.current_task
        gate_intent = routing_plan.task_intent.value if routing_plan.task_intent else None
        gate_passed = [
            s for s in scored
            if _passes_relevance_gate(task, s, enriched_files, request.current_branch, gate_intent)
        ]
        gate_excluded = [
            s for s in scored
            if not _passes_relevance_gate(task, s, enriched_files, request.current_branch, gate_intent)
        ]

        pack, trace = self._composer.compose(
            project=project,
            scored_nodes=gate_passed,
            routing_plan=routing_plan,
            include_evidence=expand_evidence,
            token_budget=budget,
            current_branch=request.current_branch,
        )

        # -- Issue 5: explicit conflict detection -----------------------------
        # Runs only over the already-composed selection — i.e. candidates that
        # already survived source-validity (Issue 1), constraint-scope
        # (Issue 2), the relevance gate above, and (within
        # detect_conflicts) an additional trust/authority + status check
        # (Issue 3/1). Bounded to this small, per-request selection — never a
        # project-wide scan — and fully retrieval-time: no new rows are
        # written, so a repeated recall() over unchanged data is deterministic.
        conflict_candidates = (
            pack.constraints + pack.architecture + pack.modules
            + pack.decisions + pack.incidents + pack.procedures
        )
        if len(conflict_candidates) >= 2:
            candidate_ids = [str(n.id) for n in conflict_candidates]
            relations = self._relations.list_by_node_ids(candidate_ids)
            conflict_map = detect_conflicts(
                conflict_candidates,
                current_branch=request.current_branch,
                relations=relations,
            )
            if conflict_map:
                for entry in trace:
                    if entry.action == "selected" and entry.memory_id in conflict_map:
                        info = conflict_map[entry.memory_id]
                        entry.conflict = info
                        # Issue 6: fold the already-computed conflict outcome
                        # into the same CompactProvenance instance referenced
                        # by pack.provenance[entry.memory_id] (composer built
                        # both from the same object) — no recomputation, no
                        # separate write path to keep in sync.
                        if entry.provenance is not None:
                            entry.provenance.conflict_status = info.resolution_status.value
                            entry.provenance.conflict_alternatives_count = len(info.alternatives)
                            entry.provenance.historical = info.own_role == "historical"

        # Append gate-excluded nodes to trace so the caller can see why they
        # were dropped (matches existing composer trace contract).
        for s in gate_excluded:
            # Build a descriptive tree_path matching the composer's convention
            depth = getattr(s.node, "depth", 0) or 0
            kind_label = s.node.kind.value.capitalize()
            if depth == 0:
                tree_path = ["Project", kind_label]
            elif depth == 1:
                tree_path = ["Project", "Subsystem", kind_label]
            else:
                tree_path = ["Project", "Subsystem", "Module", kind_label]
            trace.append(TraceEntry(
                memory_id=str(s.node.id),
                title=s.node.title,
                action="excluded",
                reason="Relevance gate: no topic-signal overlap with current task",
                score=s.score,
                score_breakdown=s.score_breakdown,
                status=s.node.status.value,
                tree_path=tree_path,
                provenance=build_provenance(
                    s.node, s.score_breakdown, current_branch=request.current_branch
                ),
            ))

        return RecallResult(
            context_pack=pack,
            routing_plan=routing_plan,
            retrieval_trace=trace,
            token_estimate=pack.token_estimate,
            recall_skipped=False,
        )

    def _load_project(self, project_id: str):  # type: ignore[return]
        orm = self._projects.get(project_id)
        if orm is None:
            raise ProjectNotFoundError(project_id)
        return orm
