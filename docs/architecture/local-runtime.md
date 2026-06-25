# Local Runtime

## Bootstrap state machine

```
UNINITIALIZED
      │
      │ MCP server starts / first tool call
      ▼
BOOTSTRAPPING
      │
      ├─ create .memory-engine/ layout
      ├─ write config.yaml (if missing)
      ├─ initialize SQLite + FTS5
      ├─ create project record
      ├─ ingest seed knowledge (README, constraints, ADRs)
      │
      ▼
READY            ◄── partial retrieval available during broader indexing
      │
      │ file changes detected on subsequent startup
      ▼
INDEXING_INCREMENTAL
      │
      ▼
READY
```

Error paths:
- Storage corrupted / permission denied → `FAILED`
- Vector backend unavailable (default) → `DEGRADED` (lexical still works)

## Project-local storage

```
<project>/.memory-engine/
├── config.yaml              ← user-editable; never overwritten on re-bootstrap
├── project_state.json       ← bootstrap_status, revisions, last_indexed_at
├── memory.db                ← SQLite: all MemoryNode, KnowledgeChunk, etc.
├── indexes/
│   ├── lexical/             ← FTS5 virtual table (inside memory.db)
│   ├── vector/              ← future: persistent vector store
│   └── manifests/
│       └── manifest.json    ← incremental index file manifest
├── cache/                   ← SimpleCache entries (in-process only, not persisted)
├── logs/                    ← future: structured logs
├── generated/
│   └── AGENT_MEMORY_POLICY.md   ← auto-generated on bootstrap
├── bootstrap/
│   └── bootstrap_report.json    ← last bootstrap outcome
├── constraints.md           ← human-authored (may be committed)
├── team-rules.md            ← human-authored (may be committed)
└── decisions.md             ← human-authored (may be committed)
```

## Incremental indexing

`IncrementalIndexCoordinator` uses a JSON file manifest at
`.memory-engine/indexes/manifests/manifest.json`.

Each entry:
```json
{
  "rel_path": "src/scheduler/retry.py",
  "content_hash": "sha256...",
  "size": 4096,
  "mtime": 1700000000.0,
  "source_type": "code_file",
  "index_status": "indexed",
  "revision": 3
}
```

Startup behavior:
1. Load manifest.
2. Walk project tree (respecting include/exclude rules).
3. Compare mtime + size → if unchanged, skip.
4. If changed → re-hash → if hash changed → re-index.
5. Entries in manifest with no file → mark as deleted (chunks set stale).
6. Git optimization: if `.git/` exists, use `git diff --name-only HEAD` to narrow scan.

## Security model

| Rule | Enforced by |
|---|---|
| No reads outside project_root | `resolve_safe()` (raises `PathEscapeError`) |
| No symlink escape | `is_safe_symlink()` |
| Binary files skipped | `is_binary()` (MIME + extension + null-byte) |
| Oversized files skipped | `is_oversized()` (default: 5 MB) |
| Default excludes | `is_excluded()` (.env, secrets/, node_modules/, .git/, etc.) |
| Redaction before persist | `redact()` in `KnowledgeIngestionService.ingest()` |
| Redaction before MCP output | `redact()` in MCP tool handlers |
| No file modification | Only `.memory-engine/` is written to |
| No auto Git commits | Never |
| No external telemetry | Never by default |

## Revision and cache strategy

`project_state.json` tracks four revision counters:

| Counter | Bumped when |
|---|---|
| `project_revision` | Git SHA or content-manifest hash |
| `index_revision` | Incremental indexing completes |
| `memory_revision` | Candidate promoted or stale marked |
| `knowledge_revision` | Document indexed or removed |

Cache key = `SHA256(project_id + task + files + symbols + budget + all four revisions)`

Stale cache entries are evicted immediately on any relevant update.
