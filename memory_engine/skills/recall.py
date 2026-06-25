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

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    EnrichedContextPack,
    MemoryNode,
    Project,
    RecallRequest,
    RecallResult,
    RoutingPlan,
    TaskIntent,
)
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.repositories.project import ProjectRepository
from memory_engine.services.memory_service import ProjectNotFoundError
from memory_engine.skills.composer import ContextComposer
from memory_engine.skills.query_analyzer import DeterministicQueryAnalyzer, QueryAnalyzerProtocol
from memory_engine.skills.ranker import DeterministicRanker
from memory_engine.skills.router import SkillRouter


class RecallService:
    def __init__(
        self,
        session: Session,
        query_analyzer: QueryAnalyzerProtocol | None = None,
    ) -> None:
        self._nodes = MemoryNodeRepository(session)
        self._projects = ProjectRepository(session)
        self._router = SkillRouter()
        self._ranker = DeterministicRanker()
        self._composer = ContextComposer()
        # Phase 4: inject analyzer (defaults to deterministic local fallback)
        self._query_analyzer: QueryAnalyzerProtocol = (
            query_analyzer or DeterministicQueryAnalyzer()
        )

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
        )

        # -- Compose context pack under budget ----------------------------
        budget = request.token_budget or routing_plan.recommended_token_budget
        expand_evidence = (
            query_analysis.evidence_expansion_required
            or routing_plan.task_intent in (TaskIntent.bug_fix, TaskIntent.test_failure)
        )

        pack, trace = self._composer.compose(
            project=project,
            scored_nodes=scored,
            routing_plan=routing_plan,
            include_evidence=expand_evidence,
            token_budget=budget,
        )

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
