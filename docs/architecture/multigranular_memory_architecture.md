# Multi-Granularity Memory Architecture (Phase 10)

## Problem statement

The original knowledge pipeline used a single fixed chunk size for both **write-time indexing** and **read-time retrieval**. This created a mismatch:

- A bug-fix query needs an *atomic constraint* ("shell=False is enforced") — a chunk of hundreds of tokens buries it in surrounding code.
- An architecture review needs a *module-level overview* — returning ten individual function blocks is noise.
- A feature implementation needs *paragraph-level context* — enough to understand the interface shape without reading whole files.

The same chunk cannot be optimally sized for all three use cases simultaneously.

---

## Design principle: separate read granularity from write granularity

Phase 10 introduces a **four-layer knowledge representation** created at write-time and selected at query-time based on intent:

```
Document
  └── Paragraph (per function / markdown heading section)
        └── Proposition (atomic factual statement)
  └── ChunkSummary (module-level or chunk-level digest)
```

The existing `KnowledgeChunk` layer is preserved unchanged for backward compatibility.

---

## Layer definitions

### Proposition (finest grain)

An atomic, independently useful factual or behavioral statement extracted deterministically (no LLM) from source content.

Sources:
- **Code**: first sentence of each docstring; high-value comments (`shell=False`, `allowlist`, `must not`, `never`, `invariant`); `raise` statements (risk propositions)
- **Markdown**: each bullet/numbered list item; sentences containing constraint keywords (`must`, `cannot`, `never`, `always`)

Proposition types and confidence levels:

| Type | Source | Confidence |
|---|---|---|
| `security_rule` | `shell=False`, `allowlist`, credential patterns | 0.85–0.99 |
| `constraint` | `must`/`cannot`/`never` sentences | 0.78–0.93 |
| `architecture` | interface/layer/boundary mentions | 0.80–0.95 |
| `decision` | `decided`/`chose`/`rationale` patterns | 0.80–0.95 |
| `risk` | `raise` statements | 0.80 |
| `test_evidence` | `assert`/`verify`/`expect` patterns | 0.72–0.87 |
| `procedure` | `step N`, `how to`, `workflow` | 0.70–0.85 |
| `implementation_detail` | default (no pattern match) | 0.70–0.85 |

**Key invariant:** every proposition is deduplication-guarded by SHA-256 of `normalized_text`. Stale propositions are marked `is_stale=True` on content re-index.

### Paragraph (local context unit)

A coherent local context block: one function/class definition in code, one heading-section in markdown.

Metadata carried:
- `symbol_names`: def/class names found in the block
- `section_heading`: function or class name (code) / heading text (markdown)
- `heading_path`: full heading nesting stack
- `source_start_line / source_end_line`: exact line range
- `paragraph_index`: position within document

Paragraphs are the expansion target for proposition hits under `paragraph_expand` policy.

### ChunkSummary (module or chunk digest)

A concise summary of one module (file) or a cluster of paragraphs, built from:
- `summary_text`: human-readable purpose statement
- `key_symbols`: up to 15 most prominent symbols
- `responsibilities`: list of per-function summaries
- `constraints_mentioned`: constraint-pattern matches in the combined content
- `important_interactions`: service/manager/resolver class references detected
- `granularity_level`: `"chunk"` | `"module"` | `"document"`

All three layers carry branch provenance (`branch_name`, `branch_scope`, `source_revision`, `commit_sha`) and stale lifecycle fields consistent with Phase 9.

---

## Write pipeline

```
raw content
    │
    ▼
redact() ─────────────────────────────────────────────
    │                                                 │
    ▼                                                 ▼
segment_paragraphs()              extract_propositions()
    │                                                 │
    ▼                                                 ▼
KnowledgeParagraphORM           KnowledgePropositionORM
knowledge_paragraphs_fts        knowledge_propositions_fts
    │
    ▼
summarize_module(paragraphs)
    │
    ▼
KnowledgeChunkSummaryORM
knowledge_summaries_fts
```

All three passes run inside a single `ingest()` call in `KnowledgeIngestionService._ingest_multigranular()`. They share the same document_id and are atomically committed.

**No LLM is called at any point.** All extraction is deterministic regex + heuristic patterns.

---

## Read pipeline

```
UnifiedRetrievalRequest
  .task_intent          → GranularityRouter
  .preferred_layers     ↗
  .proposition_types    ↗
        │
        ▼
GranularityPreference
  .preferred_layers     → ["proposition", "paragraph"]
  .expansion_policy     → "paragraph_expand"
  .proposition_types    → ["constraint", "security_rule", "risk"]
        │
        ▼
MultiGranularKnowledgeSearchService.search()
        │
        ├── fts_search_propositions()  → filter by type → _RawHit[]
        ├── fts_search_paragraphs()    → _RawHit[]
        └── fts_search_summaries()     → _RawHit[]
        │
        ▼ (expansion)
paragraph_expand: proposition hits → fetch parent KnowledgeParagraphORM
summary_overview: hits → fetch module KnowledgeChunkSummaryORM per source_path
        │
        ▼ (scoring: 5 signals, per-layer FTS rank normalization)
sorted list[MultiGranularitySearchResult]
        │
        ▼
UnifiedContextPack.multigranular_chunks
```

---

## Granularity routing table

| Intent | Preferred layers (ordered) | Expansion policy | Proposition types |
|---|---|---|---|
| `bug_fix` | proposition, paragraph | paragraph_expand | constraint, security_rule, risk |
| `test_failure` | proposition, paragraph | paragraph_expand | test_evidence, risk, constraint |
| `refactor` | paragraph, summary | paragraph_expand | architecture, decision, constraint |
| `feature_implementation` | paragraph, proposition, summary | paragraph_expand | *(all)* |
| `architecture_review` | summary, paragraph | summary_overview | architecture, decision |
| `code_explanation` | paragraph, proposition | paragraph_expand | *(all)* |
| `repository_onboarding` | summary, paragraph | summary_overview | architecture, constraint |
| `workflow_question` | proposition, paragraph | paragraph_expand | procedure, behavior |
| `documentation` | paragraph, summary | paragraph_expand | *(all)* |
| `trivial_edit` | paragraph | atomic_only | *(all)* |
| `unknown` | proposition, paragraph, summary | paragraph_expand | *(all)* |

Callers can override the router by setting `preferred_layers` and/or `proposition_types` directly on `MultiGranularSearchRequest` / `UnifiedRetrievalRequest`.

---

## Scoring formula

```
granularity_score =
    0.50 × fts_rank_normalized      (per-layer: each FTS table normalized independently)
  + 0.20 × layer_preference_bonus   (1.0 / 0.6 / 0.3 by position in preferred_layers)
  + 0.15 × proposition_type_bonus   (1.0 if type in intent filter, 0.5 neutral)
  + 0.10 × source_quality           (static: adr=1.0 ... runtime_log=0.5)
  + 0.05 × freshness_bonus          (0.0 if is_stale, 1.0 if fresh)
```

**Per-layer normalization** is critical: proposition and paragraph FTS5 ranks are not comparable in absolute value across tables. Each layer's rank range is scaled independently to `[0, 1]` before the formula is applied, preventing strong-match paragraphs from crushing proposition scores.

---

## FTS5 tables

| Table | Content indexed | Extra filters |
|---|---|---|
| `knowledge_chunks_fts` | chunk content, heading, symbols, module, tags | is_stale via KnowledgeChunk.index_status |
| `knowledge_paragraphs_fts` | paragraph content, summary, section_heading, symbols | `is_stale=0` in JOIN |
| `knowledge_propositions_fts` | proposition_text, proposition_type | `is_stale=0` in JOIN |
| `knowledge_summaries_fts` | summary_text, purpose, key_symbols, granularity_level | `is_stale=0` in JOIN, optional granularity filter |

All tables use `tokenize='porter unicode61'` for stemmed BM25 matching.

---

## Token budget split

```
total_budget (default 6000)
    ├── 60% memory        (3600 tokens) — MemoryNode recall
    └── 40% knowledge     (2400 tokens)
            ├── 75% chunk-level search  (1800 tokens) — KnowledgeSearchService
            └── 25% multigranular       ( 600 tokens) — MultiGranularKnowledgeSearchService
```

Multigranular results are returned separately in `UnifiedContextPack.multigranular_chunks` and do not displace chunk-level results.

---

## MCP interface changes (Phase 10)

`retrieve_agent_context` accepts three new optional parameters:

```python
task_intent: str = "unknown"       # routes to GranularityRouter
preferred_layers: list[str] = []   # override layer order
proposition_types: list[str] | None = None  # filter proposition type
```

The response adds:
```json
{
  "multigranular_chunks": [
    {
      "result_id": "...",
      "result_type": "proposition",
      "granularity": "proposition",
      "content": "shell=False is enforced; allowlist contains only safe commands.",
      "score": 0.91,
      "score_breakdown": { "fts_rank_normalized": 1.0, "layer_preference_bonus": 1.0, ... },
      "source_path": "memory_engine/runtime/git/git_resolver.py",
      "source_start_line": 14,
      "proposition_type_bonus": 1.0
    }
  ],
  "multigranular_results_count": 3
}
```

---

## Design choices and alternatives considered

### Why no LLM for extraction?
Deterministic extraction is faster (< 5 ms per file), reproducible (same input → same propositions), and avoids API costs. The trade-off is lower recall for implicit constraints not surfaced by regex patterns — acceptable for the primary use case (coding agent context).

### Why not Late Chunking (dense vector approach)?
Late Chunking requires an embedding model and vector store. Phase 10 targets the `lexical_structured_fallback` mode that works without any external service. The "late binding" intuition is preserved as **query-time context assembly**: the proposition is retrieved first, then expanded to its parent paragraph only when the intent calls for it.

### Why separate multigranular_chunks from knowledge_chunks?
The two layers serve different purposes in the agent's reasoning: `knowledge_chunks` provides surrounding code context; `multigranular_chunks` surfaces precise atomic facts and summaries. Keeping them separate lets the agent prioritize accordingly and avoids token pressure from merging.

### Backward compatibility
All Phase 10 tables are additive. Projects with no ingested Phase 10 data return `multigranular_chunks=[]` transparently. The existing `KnowledgeChunk` pipeline is unchanged.

---

## Related documents

- [retrieval-pipeline.md](retrieval-pipeline.md) — Phase 4–9 retrieval architecture
- [branch-aware-memory.md](branch-aware-memory.md) — Phase 9 branch-aware ranking
- [knowledge-pipeline.md](knowledge-pipeline.md) — Phase 6 knowledge ingestion
