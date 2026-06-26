"""UnifiedContextRetrievalService — fuses memory + knowledge into one ContextPack.

Context priority order:
  1. Constraints                 (memory)
  2. Project constraints         (memory)
  3. Architecture summaries      (memory)
  4. Relevant knowledge chunks   (knowledge, ranked by hybrid score)
  5. Module memories             (memory)
  6. Decisions and incidents     (memory)
  7. Procedures                  (memory)
  8. Evidence references         (memory)
  9. Retrieval trace             (both)

The service:
  - Uses RecallService for memory retrieval.
  - Uses KnowledgeSearchService for knowledge retrieval.
  - Checks the unified cache before running any retrieval.
  - Deduplicates overlapping memory summaries and knowledge snippets.
  - Enforces the token budget across both memory and knowledge sections.
  - Returns a UnifiedContextPack with a full KnowledgeTraceEntry list.

Cache invalidation is triggered externally by:
  - KnowledgeIngestionService (on ingest)
  - PromotionService / PostTaskService (on memory write)
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from memory_engine.knowledge.cache import (
    SimpleCache,
    SimpleCache as _Cache,
    get_global_cache,
    normalize_query,
)
from memory_engine.knowledge.ingestion import get_shared_vector_index
from memory_engine.knowledge.multigranular_search import MultiGranularKnowledgeSearchService
from memory_engine.knowledge.search import KnowledgeSearchService
from memory_engine.knowledge.vector_index import KnowledgeVectorIndex
from memory_engine.models.domain import (
    RecallRequest,
    TaskIntent,
)
from memory_engine.models.knowledge_domain import (
    KnowledgeContextSection,
    KnowledgeSearchRequest,
    KnowledgeTraceEntry,
    MultiGranularSearchRequest,
    MultiGranularitySearchResult,
    SourceType,
    UnifiedContextPack,
    UnifiedRetrievalRequest,
)
from memory_engine.models.orm import MemoryNodeORM
from memory_engine.skills.recall import RecallService


# Intents that benefit from knowledge retrieval
_KNOWLEDGE_INTENTS = frozenset({
    TaskIntent.bug_fix,
    TaskIntent.feature_implementation,
    TaskIntent.refactor,
    TaskIntent.test_failure,
    TaskIntent.repository_onboarding,
    TaskIntent.architecture_review,
    TaskIntent.documentation,
})

# Token budget split
_KNOWLEDGE_BUDGET_RATIO = 0.40
_MEMORY_BUDGET_RATIO = 0.60
# Phase 10: multigranular gets 25% of the knowledge budget (= 10% of total)
_MULTIGRANULAR_BUDGET_RATIO = 0.25


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _section_tokens(sec: KnowledgeContextSection) -> int:
    return _estimate_tokens(sec.content + sec.title)


class UnifiedContextRetrievalService:
    """Single entry-point for agent context retrieval.

    The agent calls retrieve() with a UnifiedRetrievalRequest.
    Internally, this fuses MemoryRecall + KnowledgeSearch + caching.
    """

    def __init__(
        self,
        session: Session,
        vector_index: KnowledgeVectorIndex | None = None,
        cache: SimpleCache | None = None,
    ) -> None:
        self._session = session
        self._vector_index: KnowledgeVectorIndex = vector_index or get_shared_vector_index()
        self._cache: _Cache = cache or get_global_cache()

    def retrieve(self, req: UnifiedRetrievalRequest) -> UnifiedContextPack:
        """Run unified memory + knowledge retrieval.

        Returns a UnifiedContextPack ready for the agent.
        All retrieval, fusion, deduplication, and token-budgeting happen here.
        """
        project_id_str = str(req.project_id)
        norm_query = normalize_query(req.task)

        # ── Cache lookup ─────────────────────────────────────────────────────
        cache_key = _Cache.make_key(
            project_id=project_id_str,
            normalized_query=norm_query,
            current_files=req.current_files,
            current_symbols=req.current_symbols,
            token_budget=req.token_budget,
            # Phase 9: branch context — separate cache entries per branch/commit
            current_branch=req.current_branch,
            head_commit=req.head_commit,
            working_tree_dirty=bool(req.modified_files),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            result = cached
            result.cache_hit = True
            return result  # type: ignore[return-value]

        t0 = time.monotonic()

        # ── Memory retrieval ─────────────────────────────────────────────────
        memory_budget = int(req.token_budget * _MEMORY_BUDGET_RATIO)
        knowledge_budget = req.token_budget - memory_budget

        recall_svc = RecallService(self._session)
        recall_req = RecallRequest(
            project_id=req.project_id,
            current_task=req.task,
            current_files=req.current_files,
            current_symbols=req.current_symbols,
            token_budget=memory_budget,
        )
        recall_result = recall_svc.recall(recall_req)
        memory_pack = recall_result.context_pack

        # Build memory trace entries
        memory_traces: list[KnowledgeTraceEntry] = []
        for te in (recall_result.retrieval_trace or []):
            memory_traces.append(KnowledgeTraceEntry(
                result_type="memory",
                result_id=te.memory_id,
                title=te.title,
                action=te.action if te.action in ("selected", "excluded") else "selected",
                score=te.score,
                score_breakdown=te.score_breakdown,
                status=te.status,
                heading_path=te.tree_path,
                reason=te.reason,
            ))

        # ── Knowledge retrieval ───────────────────────────────────────────────
        knowledge_sections: list[KnowledgeContextSection] = []
        knowledge_traces: list[KnowledgeTraceEntry] = []

        should_search_knowledge = (
            req.include_knowledge
            and _should_use_knowledge(recall_result.routing_plan.task_intent)
        )

        if should_search_knowledge:
            k_svc = KnowledgeSearchService(
                self._session, vector_index=self._vector_index, cache=self._cache
            )
            k_req = KnowledgeSearchRequest(
                project_id=req.project_id,
                query=req.task,
                current_files=req.current_files,
                current_symbols=req.current_symbols,
                source_types=req.source_type_filter,
                token_budget=knowledge_budget,
                max_results=15,
            )
            k_results = k_svc.search(k_req)

            # Deduplicate vs memory summaries
            memory_texts = _collect_memory_texts(memory_pack)
            token_used = 0
            for kr in k_results:
                if token_used >= knowledge_budget:
                    knowledge_traces.append(KnowledgeTraceEntry(
                        result_type="knowledge",
                        result_id=str(kr.chunk_id),
                        title=kr.title,
                        path=kr.source_path,
                        start_line=kr.start_line,
                        end_line=kr.end_line,
                        action="excluded",
                        score=kr.score,
                        score_breakdown=kr.score_breakdown,
                        source_type=kr.source_type.value,
                        status=kr.status.value,
                        heading_path=kr.heading_path,
                        reason="token_budget_exceeded",
                    ))
                    continue

                # Near-duplicate check vs memory content
                if _is_near_duplicate_to_memory(kr.content_preview, memory_texts):
                    knowledge_traces.append(KnowledgeTraceEntry(
                        result_type="knowledge",
                        result_id=str(kr.chunk_id),
                        title=kr.title,
                        action="excluded",
                        score=kr.score,
                        score_breakdown=kr.score_breakdown,
                        source_type=kr.source_type.value,
                        status=kr.status.value,
                        heading_path=kr.heading_path,
                        reason="near_duplicate_of_memory",
                    ))
                    continue

                tok = _estimate_tokens(kr.content_preview)
                token_used += tok
                section = KnowledgeContextSection(
                    chunk_id=str(kr.chunk_id),
                    title=kr.title,
                    source_type=kr.source_type.value,
                    source_path=kr.source_path,
                    heading_path=kr.heading_path,
                    start_line=kr.start_line,
                    end_line=kr.end_line,
                    content=kr.content_preview,
                    score=kr.score,
                    token_count=tok,
                )
                knowledge_sections.append(section)
                knowledge_traces.append(KnowledgeTraceEntry(
                    result_type="knowledge",
                    result_id=str(kr.chunk_id),
                    title=kr.title,
                    path=kr.source_path,
                    start_line=kr.start_line,
                    end_line=kr.end_line,
                    action="selected",
                    score=kr.score,
                    score_breakdown=kr.score_breakdown,
                    source_type=kr.source_type.value,
                    status=kr.status.value,
                    heading_path=kr.heading_path,
                    reason=f"hybrid_score={kr.score:.3f}",
                ))

        # ── Phase 10: multigranular retrieval ──────────────────────────────────
        multigranular_results: list[MultiGranularitySearchResult] = []
        if should_search_knowledge and _has_phase10_data(self._session, project_id_str):
            mg_budget = max(500, int(knowledge_budget * _MULTIGRANULAR_BUDGET_RATIO))
            mg_svc = MultiGranularKnowledgeSearchService(self._session)
            mg_req = MultiGranularSearchRequest(
                project_id=req.project_id,
                query=req.task,
                current_files=req.current_files,
                current_symbols=req.current_symbols,
                task_intent=req.task_intent,
                preferred_layers=req.preferred_layers,
                proposition_types=req.proposition_types,
                max_results=10,
                token_budget=mg_budget,
            )
            mg_results = mg_svc.search(mg_req)

            # Exclude results already covered by chunk-level search
            chunk_paths = {s.source_path for s in knowledge_sections if s.source_path}
            mg_token_used = 0
            for mgr in mg_results:
                if mg_token_used >= mg_budget:
                    break
                tok = _estimate_tokens(mgr.content)
                mg_token_used += tok
                multigranular_results.append(mgr)

        # ── Assemble unified pack ─────────────────────────────────────────────
        pack = _build_unified_pack(
            req=req,
            memory_pack=memory_pack,
            knowledge_sections=knowledge_sections,
            multigranular_results=multigranular_results,
            memory_traces=memory_traces,
            knowledge_traces=knowledge_traces,
        )

        # ── Cache ─────────────────────────────────────────────────────────────
        self._cache.set(cache_key, pack, project_id_str)
        return pack


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_use_knowledge(intent: TaskIntent) -> bool:
    return intent in _KNOWLEDGE_INTENTS or intent == TaskIntent.unknown


def _collect_memory_texts(memory_pack: Any) -> list[str]:
    """Collect all summary texts from a memory ContextPack."""
    texts: list[str] = []
    for attr in ("constraints", "architecture", "modules", "decisions", "incidents",
                 "procedures"):
        for item in getattr(memory_pack, attr, []):
            if hasattr(item, "summary"):
                texts.append(item.summary[:300])
    return texts


def _is_near_duplicate_to_memory(preview: str, memory_texts: list[str]) -> bool:
    """Very light overlap check — avoids duplicating content already in memory."""
    if not preview or not memory_texts:
        return False
    p_lower = preview.lower()
    for mt in memory_texts:
        shared = sum(1 for word in mt.lower().split() if word in p_lower)
        if shared / max(len(mt.split()), 1) > 0.7:
            return True
    return False


def _has_phase10_data(session: Any, project_id: str) -> bool:
    """Return True if Phase 10 tables contain records for this project."""
    try:
        from memory_engine.models.knowledge_orm import KnowledgePropositionORM
        return session.query(KnowledgePropositionORM).filter_by(
            project_id=project_id, is_stale=False
        ).limit(1).count() > 0
    except Exception:
        return False


def _build_unified_pack(
    req: UnifiedRetrievalRequest,
    memory_pack: Any,
    knowledge_sections: list[KnowledgeContextSection],
    multigranular_results: list[MultiGranularitySearchResult],
    memory_traces: list[KnowledgeTraceEntry],
    knowledge_traces: list[KnowledgeTraceEntry],
) -> UnifiedContextPack:
    mem_tokens = _estimate_tokens(
        " ".join(
            item.summary
            for attr in ("constraints", "architecture", "modules", "decisions",
                         "incidents", "procedures")
            for item in getattr(memory_pack, attr, [])
            if hasattr(item, "summary")
        )
    )
    know_tokens = sum(s.token_count for s in knowledge_sections)
    mg_tokens = sum(max(1, len(r.content) // 4) for r in multigranular_results)

    return UnifiedContextPack(
        project_id=req.project_id,
        task=req.task,
        constraints=getattr(memory_pack, "constraints", []),
        architecture=getattr(memory_pack, "architecture", []),
        modules=getattr(memory_pack, "modules", []),
        decisions=getattr(memory_pack, "decisions", []),
        incidents=getattr(memory_pack, "incidents", []),
        procedures=getattr(memory_pack, "procedures", []),
        evidence_refs=getattr(memory_pack, "evidence_refs", []),
        knowledge_chunks=knowledge_sections,
        multigranular_chunks=multigranular_results,
        retrieval_trace=memory_traces + knowledge_traces,
        memory_tokens=mem_tokens,
        knowledge_tokens=know_tokens,
        total_token_estimate=mem_tokens + know_tokens + mg_tokens,
        token_budget=req.token_budget,
        memory_results_count=sum(
            len(getattr(memory_pack, a, []))
            for a in ("constraints", "architecture", "modules", "decisions",
                      "incidents", "procedures")
        ),
        knowledge_results_count=len(knowledge_sections),
        multigranular_results_count=len(multigranular_results),
        cache_hit=False,
    )
