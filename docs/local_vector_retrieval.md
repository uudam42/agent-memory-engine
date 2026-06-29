# Local Persistent Vector Retrieval (Phase 13)

## Why lexical-only is not enough

The default engine retrieves with SQLite FTS5 (BM25). FTS5 matches **tokens**,
not **meaning**. A query for `DB schema upgrade` will not rank a chunk that says
`database migration applied at startup`, even though they describe the same
thing. For coding agents this causes silent recall misses: the relevant ADR,
constraint, or prior incident exists but is phrased differently from the query.

Phase 13 adds an optional **persistent local semantic index** so retrieval also
matches on meaning, while keeping the default install lightweight and offline.

## sqlite-vec as the persistent backend

[sqlite-vec](https://github.com/asg017/sqlite-vec) is a SQLite extension that
stores dense vectors in a virtual table and runs KNN search inside SQLite. It is
the preferred backend because it needs:

- no Docker
- no external service or daemon
- no separate database

Vectors live in `.memory-engine/vector.db`, next to `memory.db`. Two tables:

| Table | Kind | Purpose |
| --- | --- | --- |
| `vec_metadata` | regular | One row per embedded record: `project_id`, `record_type`, `branch_scope`, `lifecycle_state`, `content_hash`, `model`, `dimension`. |
| `vec_items` | `vec0` virtual | The `float[dim]` vectors, keyed by the `vec_metadata.rowid`. |

Upserts are **idempotent**: if a record's `content_hash` and `model` are
unchanged, no embedding is regenerated.

## Embedding providers (all local)

| Provider | `EMBEDDING_PROVIDER` | Install | Notes |
| --- | --- | --- | --- |
| None (default) | `none` | — | No embeddings; `semantic_similarity` stays 0.0. |
| sentence-transformers | `sentence_transformers` | `memory-engine[semantic-transformers]` | Local model, e.g. `BAAI/bge-small-en-v1.5`. |
| Ollama | `ollama` | `memory-engine[semantic-ollama]` | Talks to a local Ollama at `http://localhost:11434`. |

No cloud embedding APIs are ever called. If the configured provider is
unavailable (not installed, model missing, service down), the engine falls back
to lexical retrieval and reports `semantic_status: unavailable`.

## Hybrid retrieval flow

```
                         ┌─────────────────────────┐
        query  ────────► │  FTS5 lexical (BM25)     │ ──► ranked lexical hits
                         └─────────────────────────┘
                         ┌─────────────────────────┐
        query  ────────► │  sqlite-vec KNN (cosine) │ ──► ranked semantic hits
                         └─────────────────────────┘
                                      │
                                      ▼
                    branch / revision / lifecycle filters
                                      │
                                      ▼
                       Reciprocal Rank Fusion (RRF, k=60)
                                      │
                                      ▼
                  deterministic multi-signal ranker (unchanged)
                                      │
                                      ▼
                       token-budgeted UnifiedContextPack
```

The lexical path is always active. The semantic path is added only when enabled
and available. RRF fusion is deterministic for the same inputs and index state.

## Configuration

Top-level environment overrides (highest precedence):

```bash
export MEMORY_ENGINE_SEMANTIC_ENABLED=1
export MEMORY_ENGINE_EMBEDDING_PROVIDER=sentence_transformers   # | ollama | none
export MEMORY_ENGINE_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
export MEMORY_ENGINE_VECTOR_BACKEND=sqlite_vec
export MEMORY_ENGINE_OLLAMA_BASE_URL=http://localhost:11434
```

Programmatic defaults live in `SemanticRetrievalSettings`
(`memory_engine/config.py`): `top_k`, `fusion_mode`, `rrf_k`, `lexical_weight`,
`semantic_weight`, `ollama_timeout`.

## CLI

```bash
memory semantic status  --project-root .          # provider/backend/model + counts
memory semantic doctor  --project-root .          # availability + dimension + orphans
memory semantic reindex --project-root .          # incremental embedding
memory semantic reindex --project-root . --full   # clear + rebuild
memory semantic clear   --project-root . --confirm  # clears vectors only
```

`clear` requires `--confirm` and only removes vector data — source knowledge and
memory trees are never touched.

## MCP surface

- `memory_status` gains `semantic_enabled`, `semantic_backend`,
  `embedding_provider`, `embedding_model`, `embedded_record_count`,
  `pending_embedding_count`, `semantic_health`.
- `retrieve_agent_context` `meta` gains `semantic_backend`, `embedding_provider`,
  `embedding_model`, `semantic_status` (`disabled | unavailable | used | degraded`).
- New resource `memory://project/current/semantic-status`.

All additions are backward-compatible — existing callers are unaffected.

## Privacy guarantees

- All embedding is local (sentence-transformers in-process, or a local Ollama).
- Content is passed through the existing `redact()` filter before any vector is
  generated, so secrets never reach the embedding model or `vector.db`.
- No network calls to cloud embedding providers.

## Known limitations

- Embedding quality depends on the chosen local model.
- `sentence-transformers` downloads the model on first use (one-time, then cached
  locally). This is outside the default `uv sync`.
- Dimension changes (switching models) clear the existing vectors and require a
  reindex; `migrate_vector_tables` detects this automatically.
- sqlite-vec KNN returns L2 distance; cosine similarity assumes the provider
  emits L2-normalized vectors (sentence-transformers `normalize_embeddings=True`).
