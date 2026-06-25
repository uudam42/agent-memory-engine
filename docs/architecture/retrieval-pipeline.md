# Retrieval Pipeline

## Retrieval modes

| Mode | When active | Signals used |
|---|---|---|
| `hybrid_lexical_vector` | Persistent vector backend healthy | FTS5 + vector cosine, RRF fusion |
| `lexical_structured_fallback` | No persistent vector (default local) | FTS5, module-path, symbol, tree proximity, importance, confidence, freshness |

Default mode: `lexical_structured_fallback` (no Qdrant, no Docker required).

---

## UnifiedContextRetrievalService.retrieve()

```
UnifiedRetrievalRequest(project_id, task, current_files, current_symbols, token_budget)
       │
       ▼
 Cache lookup (SimpleCache, TTL=5min, key=SHA256(project+task+files+symbols+budget))
       │ hit → return cached UnifiedContextPack
       │ miss ↓
       ▼
 QueryAnalyzer.analyze(task, current_files, current_symbols)
       │ → TaskIntent, module_paths, symbol_hints, keywords
       │
       ├──────────────────────────────────────────────┐
       ▼                                              ▼
 RecallService.recall()                 KnowledgeSearchService.search()
       │                                              │
       │ SQL: MemoryNodeORM                          │ FTS5 MATCH + InMemoryVector
       │ filters: status=active,                     │ RRF(k=60) fusion
       │   project_id, intent-kind mapping           │ source quality weights
       │                                              │
       ▼                                              ▼
 DeterministicRanker.rank()             KnowledgeSearchResult list
       │                                              │
       │ signals (weighted):                         │ dedup by preview similarity
       │  semantic_similarity  0.30                  │ trim to token budget
       │  module_path_overlap  0.25                  │
       │  tag_overlap          0.20                  │
       │  importance           0.15                  │
       │  freshness            0.10                  │
       │
       ▼
 ContextComposer.compose()
       │ groups results by kind (constraint, module, decision, incident, procedure)
       │ trims to memory token budget (60% of total)
       │
       ▼
 _is_near_duplicate_to_memory() dedup
       │
       ▼
 UnifiedContextPack
       │ constraints, architecture, modules, decisions, incidents, procedures
       │ knowledge_chunks (40% of token budget)
       │ retrieval_trace (per-signal score breakdown)
       │ total_token_estimate, cache_hit, memory/knowledge result counts
       │
       ▼
 Cache.set(key, pack, project_id)
```

---

## RRF fusion (Reciprocal Rank Fusion)

For knowledge retrieval:

```
score_RRF(doc) = Σ  1 / (k + rank_i(doc))
                lists
```

`k = 60` (default). Higher k → less sensitive to top-rank position.

Applied across:
1. FTS5 results (ranked by BM25 score)
2. InMemoryVector results (ranked by cosine similarity)

Then re-ranked by composite score:

```
W_VECTOR        = 0.30
W_LEXICAL       = 0.25
W_MODULE        = 0.15
W_SYMBOL        = 0.10
W_FRESHNESS     = 0.10
W_SOURCE_QUALITY= 0.05
W_EVIDENCE_LINK = 0.05
```

Source quality per type (descending):
`adr=1.0, architecture_doc=0.95, code_file=0.90, readme=0.85, test_report=0.80, ...`

---

## Token budget strategy

Default: 6000 tokens total.

Split:
- Memory: 60% (3600 tokens)
- Knowledge: 40% (2400 tokens)

Memory groups (in order, trimmed if over budget):
1. constraints (highest priority)
2. architecture
3. incidents (high importance)
4. decisions
5. procedures
6. modules

---

## Retrieval trace

Each `ContextPack` includes `retrieval_trace: list[TraceEntry]`:

```python
class TraceEntry(BaseModel):
    node_id: str
    title: str
    kind: str
    module_paths: list[str]
    score: float
    signal_breakdown: dict[str, float]
    rank: int
    included: bool
    exclusion_reason: str | None
```

Agents can use the trace to verify the relevance of returned context.
