"""MultiGranularKnowledgeSearchService — hierarchical multi-layer retrieval.

Phase 10: Searches proposition/paragraph/summary FTS5 tables at the granularity
preferred by the query intent, then merges and ranks results.

Scoring formula (all weights sum to 1.0):
  granularity_score =
    0.50 * fts_rank_normalized      (higher rank → closer to 1.0)
  + 0.20 * layer_preference_bonus   (1.0 for 1st preferred layer, 0.6 for 2nd, 0.3 for 3rd)
  + 0.15 * proposition_type_bonus   (1.0 if prop type in intent's preferred list)
  + 0.10 * source_quality           (static per source_type)
  + 0.05 * freshness_bonus          (0.0 if is_stale, 1.0 if fresh)

Expansion:
  paragraph_expand  — for each proposition hit, also fetch its parent paragraph
                      (attached as expanded_from reference, not a separate scored result)
  summary_overview  — after layer search, attach module-level summary for each
                      unique source_path found
  atomic_only       — no expansion; return matched units only
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from memory_engine.knowledge.fts_index import (
    fts_search_paragraphs,
    fts_search_propositions,
    fts_search_summaries,
)
from memory_engine.knowledge.granularity_router import GranularityPreference, GranularityRouter
from memory_engine.models.knowledge_domain import (
    GranularityLevel,
    MultiGranularSearchRequest,
    MultiGranularitySearchResult,
)
from memory_engine.models.knowledge_orm import (
    KnowledgeChunkSummaryORM,
    KnowledgeParagraphORM,
    KnowledgePropositionORM,
)

# Static source quality weights (same scale as KnowledgeSearchService)
_SOURCE_QUALITY: dict[str, float] = {
    "adr": 1.0,
    "architecture_doc": 0.95,
    "api_spec": 0.90,
    "readme": 0.85,
    "code_file": 0.80,
    "test_report": 0.75,
    "manual_note": 0.70,
    "markdown": 0.70,
    "task_artifact": 0.65,
    "code_comment": 0.65,
    "git_diff": 0.60,
    "runtime_log": 0.50,
}

# Layer preference multipliers (index = position in preferred_layers list)
_LAYER_PREF_BONUS = [1.0, 0.6, 0.3]

# Scoring weights
W_FTS_RANK = 0.50
W_LAYER_PREF = 0.20
W_PROP_TYPE = 0.15
W_SOURCE_QUALITY = 0.10
W_FRESHNESS = 0.05


def _normalize_rank(rank: float, min_rank: float, max_rank: float) -> float:
    """Normalize an FTS5 rank (negative, closer to 0 = better) into [0, 1].

    Normalizes per-layer so ranks from different FTS tables are comparable within
    their own scale before merging.
    """
    if max_rank == min_rank:
        return 1.0   # single result (or all equal) → treat as best
    # rank in [min_rank, max_rank]; max_rank is closest to 0 (best)
    return (rank - min_rank) / (max_rank - min_rank)


def _source_quality(source_path: str | None) -> float:
    if not source_path:
        return 0.5
    ext = source_path.rsplit(".", 1)[-1].lower()
    for key, val in _SOURCE_QUALITY.items():
        if key in source_path.lower():
            return val
    return 0.70 if ext in ("py", "ts", "js") else 0.60


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class _RawHit:
    result_id: str
    layer: str
    fts_rank: float
    content: str
    source_path: str | None
    source_start_line: int | None
    source_end_line: int | None
    branch_name: str | None
    branch_scope: str | None
    commit_sha: str | None
    is_stale: bool
    parent_id: str | None
    proposition_type: str | None = None    # only for proposition layer
    granularity_level: str | None = None   # only for summary layer


class MultiGranularKnowledgeSearchService:
    """Multi-stage retrieval across proposition/paragraph/summary layers.

    Instantiate per-request; Session must be externally managed.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._router = GranularityRouter()

    def search(self, req: MultiGranularSearchRequest) -> list[MultiGranularitySearchResult]:
        """Run multi-layer search and return ranked, deduplicated results."""
        project_id_str = str(req.project_id)
        pref = self._router.route(req.task_intent)

        # Allow the caller to override router decisions.
        # When preferred_layers is overridden, clear the intent's proposition_types filter
        # (the caller chose the layer; they didn't ask for a type sub-filter).
        # The caller can still supply explicit proposition_types to re-enable filtering.
        if req.preferred_layers:
            pref = GranularityPreference(
                preferred_layers=req.preferred_layers,
                expansion_policy=pref.expansion_policy,
                proposition_types=req.proposition_types,   # None unless caller is explicit
                max_per_layer=pref.max_per_layer,
            )
        elif req.proposition_types is not None:
            pref = GranularityPreference(
                preferred_layers=pref.preferred_layers,
                expansion_policy=pref.expansion_policy,
                proposition_types=req.proposition_types,
                max_per_layer=pref.max_per_layer,
            )

        # ------------------------------------------------------------------
        # Stage 1: FTS5 search on each preferred layer
        # ------------------------------------------------------------------
        raw_hits: list[_RawHit] = []
        seen_ids: set[str] = set()

        for layer in pref.preferred_layers:
            layer_hits = self._search_layer(
                layer=layer,
                query=req.query,
                project_id_str=project_id_str,
                pref=pref,
                include_stale=req.include_stale,
            )
            for hit in layer_hits:
                if hit.result_id not in seen_ids:
                    seen_ids.add(hit.result_id)
                    raw_hits.append(hit)

        if not raw_hits:
            return []

        # ------------------------------------------------------------------
        # Stage 2: Expansion (paragraph_expand / summary_overview)
        # ------------------------------------------------------------------
        if pref.expansion_policy == "paragraph_expand":
            raw_hits = self._expand_propositions(raw_hits, project_id_str, seen_ids)
        elif pref.expansion_policy == "summary_overview":
            raw_hits = self._attach_module_summaries(raw_hits, project_id_str, seen_ids)

        # ------------------------------------------------------------------
        # Stage 3: Score + rank
        # ------------------------------------------------------------------
        scored = self._score_hits(raw_hits, pref)

        # ------------------------------------------------------------------
        # Stage 4: Token-budget trim + de-duplicate by content prefix
        # ------------------------------------------------------------------
        seen_content: set[str] = set()
        results: list[MultiGranularitySearchResult] = []
        token_used = 0
        for hit, score, breakdown in scored[: req.max_results * 2]:
            if token_used >= req.token_budget:
                break
            prefix = hit.content[:150]
            if prefix in seen_content:
                continue
            seen_content.add(prefix)

            tok = _tokens(hit.content)
            token_used += tok

            results.append(MultiGranularitySearchResult(
                result_id=hit.result_id,
                result_type=hit.layer,
                granularity=self._granularity_level(hit),
                content=hit.content,
                score=round(score, 4),
                score_breakdown=breakdown,
                source_path=hit.source_path,
                source_start_line=hit.source_start_line,
                source_end_line=hit.source_end_line,
                branch_name=hit.branch_name,
                branch_scope=hit.branch_scope,
                commit_sha=hit.commit_sha,
                is_stale=hit.is_stale,
                parent_id=hit.parent_id,
                selection_reason=f"layer={hit.layer}, score={score:.3f}",
                expanded_from=None,
            ))
            if len(results) >= req.max_results:
                break

        return results

    # -----------------------------------------------------------------------
    # Layer search helpers
    # -----------------------------------------------------------------------

    def _search_layer(
        self,
        layer: str,
        query: str,
        project_id_str: str,
        pref: GranularityPreference,
        include_stale: bool,
    ) -> list[_RawHit]:
        if layer == "proposition":
            return self._search_propositions(query, project_id_str, pref, include_stale)
        if layer == "paragraph":
            return self._search_paragraphs(query, project_id_str, include_stale)
        if layer == "summary":
            return self._search_summaries(query, project_id_str, include_stale)
        return []

    def _search_propositions(
        self,
        query: str,
        project_id_str: str,
        pref: GranularityPreference,
        include_stale: bool,
    ) -> list[_RawHit]:
        raw = fts_search_propositions(self._session, project_id_str, query, limit=pref.max_per_layer)
        prop_ids = [r.proposition_id for r in raw]
        if not prop_ids:
            return []

        orm_rows: list[KnowledgePropositionORM] = (
            self._session.query(KnowledgePropositionORM)
            .filter(KnowledgePropositionORM.proposition_id.in_(prop_ids))
            .all()
        )
        orm_map = {r.proposition_id: r for r in orm_rows}

        # Build rank lookup
        rank_map = {r.proposition_id: r.rank for r in raw}

        hits: list[_RawHit] = []
        for pid in prop_ids:
            orm = orm_map.get(pid)
            if orm is None:
                continue
            if orm.is_stale and not include_stale:
                continue
            if pref.proposition_types and orm.proposition_type not in pref.proposition_types:
                continue
            hits.append(_RawHit(
                result_id=orm.proposition_id,
                layer="proposition",
                fts_rank=rank_map.get(pid, -1.0),
                content=orm.proposition_text,
                source_path=orm.source_path,
                source_start_line=orm.source_start_line,
                source_end_line=orm.source_end_line,
                branch_name=orm.branch_name,
                branch_scope=orm.branch_scope,
                commit_sha=orm.commit_sha,
                is_stale=orm.is_stale,
                parent_id=orm.paragraph_id,
                proposition_type=orm.proposition_type,
            ))
        return hits

    def _search_paragraphs(
        self,
        query: str,
        project_id_str: str,
        include_stale: bool,
    ) -> list[_RawHit]:
        raw = fts_search_paragraphs(self._session, project_id_str, query, limit=10)
        para_ids = [r.paragraph_id for r in raw]
        if not para_ids:
            return []

        orm_rows: list[KnowledgeParagraphORM] = (
            self._session.query(KnowledgeParagraphORM)
            .filter(KnowledgeParagraphORM.paragraph_id.in_(para_ids))
            .all()
        )
        orm_map = {r.paragraph_id: r for r in orm_rows}
        rank_map = {r.paragraph_id: r.rank for r in raw}

        hits: list[_RawHit] = []
        for pid in para_ids:
            orm = orm_map.get(pid)
            if orm is None:
                continue
            if orm.is_stale and not include_stale:
                continue
            hits.append(_RawHit(
                result_id=orm.paragraph_id,
                layer="paragraph",
                fts_rank=rank_map.get(pid, -1.0),
                content=orm.content,
                source_path=orm.source_path,
                source_start_line=orm.source_start_line,
                source_end_line=orm.source_end_line,
                branch_name=orm.branch_name,
                branch_scope=orm.branch_scope,
                commit_sha=orm.commit_sha,
                is_stale=orm.is_stale,
                parent_id=None,
            ))
        return hits

    def _search_summaries(
        self,
        query: str,
        project_id_str: str,
        include_stale: bool,
    ) -> list[_RawHit]:
        raw = fts_search_summaries(self._session, project_id_str, query, limit=10)
        summ_ids = [r.summary_id for r in raw]
        if not summ_ids:
            return []

        orm_rows: list[KnowledgeChunkSummaryORM] = (
            self._session.query(KnowledgeChunkSummaryORM)
            .filter(KnowledgeChunkSummaryORM.summary_id.in_(summ_ids))
            .all()
        )
        orm_map = {r.summary_id: r for r in orm_rows}
        rank_map = {r.summary_id: r.rank for r in raw}

        hits: list[_RawHit] = []
        for sid in summ_ids:
            orm = orm_map.get(sid)
            if orm is None:
                continue
            if orm.is_stale and not include_stale:
                continue
            hits.append(_RawHit(
                result_id=orm.summary_id,
                layer="summary",
                fts_rank=rank_map.get(sid, -1.0),
                content=orm.summary_text,
                source_path=orm.source_path,
                source_start_line=orm.source_start_line,
                source_end_line=orm.source_end_line,
                branch_name=orm.branch_name,
                branch_scope=orm.branch_scope,
                commit_sha=orm.commit_sha,
                is_stale=orm.is_stale,
                parent_id=None,
                granularity_level=orm.granularity_level,
            ))
        return hits

    # -----------------------------------------------------------------------
    # Expansion helpers
    # -----------------------------------------------------------------------

    def _expand_propositions(
        self,
        hits: list[_RawHit],
        project_id_str: str,
        seen_ids: set[str],
    ) -> list[_RawHit]:
        """For proposition hits that have a parent_id, fetch the parent paragraph."""
        para_ids_needed = {
            h.parent_id for h in hits
            if h.layer == "proposition" and h.parent_id and h.parent_id not in seen_ids
        }
        if not para_ids_needed:
            return hits

        orm_rows: list[KnowledgeParagraphORM] = (
            self._session.query(KnowledgeParagraphORM)
            .filter(
                KnowledgeParagraphORM.paragraph_id.in_(para_ids_needed),
                KnowledgeParagraphORM.is_stale == False,  # noqa: E712
            )
            .all()
        )
        expanded: list[_RawHit] = list(hits)
        for orm in orm_rows:
            if orm.paragraph_id in seen_ids:
                continue
            seen_ids.add(orm.paragraph_id)
            expanded.append(_RawHit(
                result_id=orm.paragraph_id,
                layer="paragraph",
                fts_rank=-0.5,      # synthetic rank for expanded results
                content=orm.content,
                source_path=orm.source_path,
                source_start_line=orm.source_start_line,
                source_end_line=orm.source_end_line,
                branch_name=orm.branch_name,
                branch_scope=orm.branch_scope,
                commit_sha=orm.commit_sha,
                is_stale=orm.is_stale,
                parent_id=None,
            ))
        return expanded

    def _attach_module_summaries(
        self,
        hits: list[_RawHit],
        project_id_str: str,
        seen_ids: set[str],
    ) -> list[_RawHit]:
        """For summary_overview policy: attach module-level summaries for each unique source_path."""
        source_paths = {h.source_path for h in hits if h.source_path}
        if not source_paths:
            return hits

        orm_rows: list[KnowledgeChunkSummaryORM] = (
            self._session.query(KnowledgeChunkSummaryORM)
            .filter(
                KnowledgeChunkSummaryORM.project_id == project_id_str,
                KnowledgeChunkSummaryORM.source_path.in_(source_paths),
                KnowledgeChunkSummaryORM.granularity_level == "module",
                KnowledgeChunkSummaryORM.is_stale == False,  # noqa: E712
            )
            .all()
        )
        expanded = list(hits)
        for orm in orm_rows:
            if orm.summary_id in seen_ids:
                continue
            seen_ids.add(orm.summary_id)
            expanded.append(_RawHit(
                result_id=orm.summary_id,
                layer="summary",
                fts_rank=-0.5,
                content=orm.summary_text,
                source_path=orm.source_path,
                source_start_line=orm.source_start_line,
                source_end_line=orm.source_end_line,
                branch_name=orm.branch_name,
                branch_scope=orm.branch_scope,
                commit_sha=orm.commit_sha,
                is_stale=orm.is_stale,
                parent_id=None,
                granularity_level=orm.granularity_level,
            ))
        return expanded

    # -----------------------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------------------

    def _score_hits(
        self,
        hits: list[_RawHit],
        pref: GranularityPreference,
    ) -> list[tuple[_RawHit, float, dict[str, float]]]:
        """Score each hit using the 5-signal formula; return sorted list.

        FTS5 rank is normalized per-layer so proposition/paragraph/summary scores
        are not crushed when their absolute rank values differ across tables.
        """
        # Per-layer rank bounds for normalization
        layer_min: dict[str, float] = {}
        layer_max: dict[str, float] = {}
        for h in hits:
            layer_min[h.layer] = min(layer_min.get(h.layer, 0.0), h.fts_rank)
            layer_max[h.layer] = max(layer_max.get(h.layer, -1e9), h.fts_rank)

        scored: list[tuple[_RawHit, float, dict[str, float]]] = []
        for hit in hits:
            # 1. FTS rank (normalized per-layer)
            fts_norm = _normalize_rank(
                hit.fts_rank,
                layer_min.get(hit.layer, hit.fts_rank),
                layer_max.get(hit.layer, hit.fts_rank),
            )

            # 2. Layer preference bonus
            try:
                idx = pref.preferred_layers.index(hit.layer)
                pref_bonus = _LAYER_PREF_BONUS[min(idx, len(_LAYER_PREF_BONUS) - 1)]
            except ValueError:
                pref_bonus = 0.2   # not in preferred list

            # 3. Proposition type bonus
            if hit.proposition_type and pref.proposition_types:
                type_bonus = 1.0 if hit.proposition_type in pref.proposition_types else 0.0
            else:
                type_bonus = 0.5   # neutral when no type filter

            # 4. Source quality
            sq = _source_quality(hit.source_path)

            # 5. Freshness
            fresh = 0.0 if hit.is_stale else 1.0

            score = (
                W_FTS_RANK * fts_norm
                + W_LAYER_PREF * pref_bonus
                + W_PROP_TYPE * type_bonus
                + W_SOURCE_QUALITY * sq
                + W_FRESHNESS * fresh
            )

            breakdown = {
                "fts_rank_normalized": round(fts_norm, 4),
                "layer_preference_bonus": round(pref_bonus, 4),
                "proposition_type_bonus": round(type_bonus, 4),
                "source_quality": round(sq, 4),
                "freshness_bonus": round(fresh, 4),
                "total": round(score, 4),
            }
            scored.append((hit, score, breakdown))

        scored.sort(key=lambda t: t[1], reverse=True)
        return scored

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    @staticmethod
    def _granularity_level(hit: _RawHit) -> GranularityLevel:
        if hit.layer == "proposition":
            return GranularityLevel.proposition
        if hit.layer == "paragraph":
            return GranularityLevel.paragraph
        if hit.granularity_level == "module":
            return GranularityLevel.module
        if hit.granularity_level == "document":
            return GranularityLevel.document
        return GranularityLevel.chunk
