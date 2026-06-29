"""KnowledgeSearchService — hybrid lexical + vector search with RRF fusion.

Scoring model:
  knowledge_score =
    0.30 * dense_semantic_similarity
  + 0.25 * lexical_similarity          (normalized FTS5 rank)
  + 0.15 * module_path_overlap
  + 0.10 * symbol_overlap
  + 0.10 * freshness
  + 0.05 * source_quality
  + 0.05 * evidence_link_strength      (placeholder: 0.5 for now)

RRF fusion:
  rrf_score(doc, k=60) = 1 / (k + rank)
  Final hybrid score: 0.5 * rrf_fts + 0.5 * rrf_vec
  Then the above per-signal bonuses are additive on top.

Output:
  list[KnowledgeSearchResult] sorted by hybrid score, de-duplicated,
  token-budget trimmed.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from memory_engine.knowledge.cache import (
    SimpleCache,
    get_global_cache,
    normalize_query,
)
from memory_engine.knowledge.fts_index import fts_search
from memory_engine.knowledge.ingestion import get_shared_vector_index
from memory_engine.knowledge.vector_index import InMemoryVectorIndex, KnowledgeVectorIndex
from memory_engine.models.knowledge_domain import (
    ChunkIndexStatus,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    SourceType,
)
from memory_engine.models.knowledge_orm import KnowledgeChunkORM, KnowledgeDocumentORM

# ── Scoring weights ──────────────────────────────────────────────────────────
W_VECTOR = 0.30
W_LEXICAL = 0.25
W_MODULE = 0.15
W_SYMBOL = 0.10
W_FRESHNESS = 0.10
W_SOURCE_QUALITY = 0.05
W_EVIDENCE_LINK = 0.05

# Source quality (static, reflects how reliable/curated the source is)
_SOURCE_QUALITY: dict[str, float] = {
    "adr": 1.0,
    "architecture_doc": 0.95,
    "api_spec": 0.90,
    "readme": 0.85,
    "code_file": 0.80,
    "test_report": 0.75,
    "manual_note": 0.70,
    "task_artifact": 0.65,
    "markdown": 0.70,
    "code_comment": 0.65,
    "git_diff": 0.60,
    "runtime_log": 0.50,
}

# RRF k parameter
_RRF_K = 60


def _rrf(rank: int) -> float:
    return 1.0 / (_RRF_K + rank)


@dataclass
class _SemVecHit:
    """Lightweight adapter so persistent VectorSearchResult fits the RRF loop."""

    chunk_id: str
    score: float


def _module_overlap(chunk_modules: list[str], query_files: list[str]) -> float:
    if not chunk_modules or not query_files:
        return 0.0
    chunk_set = set(chunk_modules)
    # Convert file paths to dotted module paths for comparison
    query_modules = set()
    for f in query_files:
        m = re.sub(r"[/\\]", ".", f.rstrip("/"))
        m = re.sub(r"\.py$", "", m)
        query_modules.add(m)
        # Also add prefixes
        parts = m.split(".")
        for i in range(1, len(parts)):
            query_modules.add(".".join(parts[:i]))
    if not query_modules:
        return 0.0
    overlap = len(chunk_set & query_modules) / max(len(chunk_set), 1)
    return min(1.0, overlap)


def _symbol_overlap(chunk_symbols: list[str], query_symbols: list[str]) -> float:
    if not chunk_symbols or not query_symbols:
        return 0.0
    cs = set(s.lower() for s in chunk_symbols)
    qs = set(s.lower() for s in query_symbols)
    return min(1.0, len(cs & qs) / max(len(cs), 1))


def _freshness(status: str) -> float:
    return 0.9 if status == "indexed" else 0.2


def _content_preview(content: str, max_chars: int = 400) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars].rstrip() + "…"


@dataclass
class _ScoredCandidate:
    chunk_id: str
    document_id: str
    vector_score: float = 0.0
    semantic_score: float = 0.0   # Phase 13: real cosine from persistent backend
    lexical_score: float = 0.0
    module_score: float = 0.0
    symbol_score: float = 0.0
    freshness_score: float = 0.5
    source_quality: float = 0.5
    evidence_link_score: float = 0.5
    # from DB
    content: str = ""
    heading_path: list[str] = field(default_factory=list)
    module_paths: list[str] = field(default_factory=list)
    related_symbols: list[str] = field(default_factory=list)
    source_type: str = "manual_note"
    source_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    language: str | None = None
    index_status: str = "indexed"
    doc_title: str = ""
    doc_version_ref: str | None = None

    @property
    def final_score(self) -> float:
        return (
            W_VECTOR * self.vector_score
            + W_LEXICAL * self.lexical_score
            + W_MODULE * self.module_score
            + W_SYMBOL * self.symbol_score
            + W_FRESHNESS * self.freshness_score
            + W_SOURCE_QUALITY * self.source_quality
            + W_EVIDENCE_LINK * self.evidence_link_score
        )

    @property
    def breakdown(self) -> dict[str, float]:
        return {
            "vector": round(self.vector_score, 3),
            "semantic_similarity": round(self.semantic_score, 3),
            "lexical": round(self.lexical_score, 3),
            "module_overlap": round(self.module_score, 3),
            "symbol_overlap": round(self.symbol_score, 3),
            "freshness": round(self.freshness_score, 3),
            "source_quality": round(self.source_quality, 3),
            "evidence_link": round(self.evidence_link_score, 3),
        }


class KnowledgeSearchService:
    """Hybrid lexical + vector search for KnowledgeChunks.

    Fully deterministic given the same inputs and index state.
    """

    def __init__(
        self,
        session: Session,
        vector_index: KnowledgeVectorIndex | None = None,
        cache: SimpleCache | None = None,
        semantic_index=None,  # type: ignore[no-untyped-def]  # Phase 13: SqliteVecIndex | None
        branch_name: str | None = None,
    ) -> None:
        self._session = session
        self._vector_index: KnowledgeVectorIndex = vector_index or get_shared_vector_index()
        self._cache = cache or get_global_cache()
        # Phase 13: optional persistent semantic backend (SqliteVecIndex).
        # When present, real cosine similarity replaces the ephemeral BoW score.
        self._semantic_index = semantic_index
        self._branch_name = branch_name

    def search(self, req: KnowledgeSearchRequest) -> list[KnowledgeSearchResult]:
        project_id_str = str(req.project_id)
        norm_query = normalize_query(req.query)

        # ── Cache lookup ─────────────────────────────────────────────────────
        cache_key = SimpleCache.make_key(
            project_id=project_id_str,
            normalized_query=norm_query,
            current_files=req.current_files,
            current_symbols=req.current_symbols,
            token_budget=req.token_budget,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        # ── 1. Metadata filter ────────────────────────────────────────────────
        filter_meta: dict = {}
        if req.source_types:
            filter_meta["source_type"] = [st.value for st in req.source_types]

        # ── 2. Parallel lexical + vector search ──────────────────────────────
        fts_hits = fts_search(self._session, project_id_str, norm_query, limit=req.max_results * 3)

        # Phase 13: prefer the persistent sqlite-vec backend when available.
        # It returns real cosine similarity (record_id == chunk_id for chunks).
        # When absent, fall back to the ephemeral BoW vector index unchanged.
        semantic_scores: dict[str, float] = {}
        if self._semantic_index is not None:
            try:
                sem_hits = self._semantic_index.search(
                    norm_query, project_id_str,
                    limit=req.max_results * 3,
                    branch_name=self._branch_name,
                    record_types=["chunk"],
                )
            except Exception:
                sem_hits = []
            vec_hits = [
                _SemVecHit(chunk_id=h.record_id, score=h.score) for h in sem_hits
            ]
            for h in sem_hits:
                semantic_scores[h.record_id] = h.score
        else:
            vec_hits = self._vector_index.search(
                norm_query, project_id_str,
                limit=req.max_results * 3,
                filter_metadata=filter_meta if filter_meta else None,
            )

        # ── 3. RRF fusion ─────────────────────────────────────────────────────
        candidates: dict[str, _ScoredCandidate] = {}

        # Lexical results
        # FTS5 rank is negative (more negative = better); normalize to [0,1]
        fts_min_rank = min((h.rank for h in fts_hits), default=-1.0) or -1.0
        for rank_i, hit in enumerate(fts_hits):
            # Normalize: rank 0 (best) → 1.0, rank fts_min → 0.0
            lex_score = 1.0 - (hit.rank / fts_min_rank) if fts_min_rank != 0 else 0.5
            if hit.chunk_id not in candidates:
                candidates[hit.chunk_id] = _ScoredCandidate(
                    chunk_id=hit.chunk_id, document_id=""
                )
            candidates[hit.chunk_id].lexical_score = max(
                candidates[hit.chunk_id].lexical_score,
                _rrf(rank_i) * 2  # scale RRF to useful range
            )

        # Vector results
        for rank_i, hit in enumerate(vec_hits):
            if hit.chunk_id not in candidates:
                candidates[hit.chunk_id] = _ScoredCandidate(
                    chunk_id=hit.chunk_id, document_id=""
                )
            candidates[hit.chunk_id].vector_score = max(
                candidates[hit.chunk_id].vector_score,
                hit.score,
            )
            if hit.chunk_id in semantic_scores:
                candidates[hit.chunk_id].semantic_score = max(
                    candidates[hit.chunk_id].semantic_score,
                    semantic_scores[hit.chunk_id],
                )

        if not candidates:
            return []

        # ── 4. Enrich from DB ──────────────────────────────────────────────────
        chunk_ids = list(candidates.keys())
        db_chunks: list[KnowledgeChunkORM] = (
            self._session.query(KnowledgeChunkORM)
            .filter(KnowledgeChunkORM.chunk_id.in_(chunk_ids))
            .all()
        )
        doc_ids = list({c.document_id for c in db_chunks})
        db_docs: dict[str, KnowledgeDocumentORM] = {
            d.document_id: d
            for d in self._session.query(KnowledgeDocumentORM)
                .filter(KnowledgeDocumentORM.document_id.in_(doc_ids))
                .all()
        }

        # Apply source_type filter (DB-level for FTS hits that may have slipped through)
        allowed_types = {st.value for st in req.source_types} if req.source_types else None

        for chunk in db_chunks:
            if allowed_types and chunk.source_type not in allowed_types:
                candidates.pop(chunk.chunk_id, None)
                continue
            # Only include active indexed chunks
            if chunk.index_status == "stale":
                candidates.pop(chunk.chunk_id, None)
                continue

            doc = db_docs.get(chunk.document_id)
            cand = candidates[chunk.chunk_id]
            cand.document_id = chunk.document_id
            cand.content = chunk.content
            cand.heading_path = chunk.heading_path or []
            cand.module_paths = chunk.module_paths or []
            cand.related_symbols = chunk.related_symbols or []
            cand.source_type = chunk.source_type
            cand.source_path = chunk.source_path
            cand.start_line = chunk.start_line
            cand.end_line = chunk.end_line
            cand.language = chunk.language
            cand.index_status = chunk.index_status
            cand.doc_title = doc.title if doc else "Unknown"
            cand.doc_version_ref = doc.version_ref if doc else None

            # Per-signal bonuses
            cand.module_score = _module_overlap(cand.module_paths, req.current_files)
            cand.symbol_score = _symbol_overlap(cand.related_symbols, req.current_symbols)
            cand.freshness_score = _freshness(cand.index_status)
            cand.source_quality = _SOURCE_QUALITY.get(chunk.source_type, 0.5)
            cand.evidence_link_score = 0.5  # placeholder

        # Remove any candidates that didn't find a matching DB row
        candidates = {k: v for k, v in candidates.items() if v.content}

        # ── 5. Sort + de-duplicate near-identical content ───────────────────────
        sorted_cands = sorted(candidates.values(), key=lambda c: c.final_score, reverse=True)
        seen_hashes: set[str] = set()
        deduped: list[_ScoredCandidate] = []
        for cand in sorted_cands:
            h = cand.content[:200]  # rough content fingerprint
            if h not in seen_hashes:
                seen_hashes.add(h)
                deduped.append(cand)

        # ── 6. Token-budget trim ────────────────────────────────────────────────
        results: list[KnowledgeSearchResult] = []
        token_used = 0
        for cand in deduped[: req.max_results]:
            tok = max(1, len(cand.content) // 4)
            if token_used + tok > req.token_budget:
                break
            token_used += tok

            heading_title = " > ".join(cand.heading_path) if cand.heading_path else cand.doc_title

            results.append(KnowledgeSearchResult(
                chunk_id=uuid.UUID(cand.chunk_id),
                document_id=uuid.UUID(cand.document_id) if cand.document_id else uuid.uuid4(),
                title=f"{cand.doc_title}: {heading_title}" if heading_title != cand.doc_title else cand.doc_title,
                source_path=cand.source_path,
                source_type=SourceType(cand.source_type),
                heading_path=cand.heading_path,
                language=cand.language,
                start_line=cand.start_line,
                end_line=cand.end_line,
                content_preview=_content_preview(cand.content),
                module_paths=cand.module_paths,
                related_symbols=cand.related_symbols,
                score=round(cand.final_score, 4),
                score_breakdown=cand.breakdown,
                status=ChunkIndexStatus(cand.index_status),
                version_ref=cand.doc_version_ref,
            ))

        # ── 7. Cache result ─────────────────────────────────────────────────────
        self._cache.set(cache_key, results, project_id_str)

        return results
