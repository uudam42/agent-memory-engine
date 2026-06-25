# Git-Aware Incremental Synchronization

## Overview

Phase 9 introduces Git-aware incremental synchronization. The memory engine now detects Git state at retrieval time and uses it to narrow the file-change scan, annotate indexed knowledge with branch metadata, and invalidate stale cache entries when the working tree changes.

## Architecture

```
ProjectContext.get_git_context()
        │
        ▼
GitContextResolver.resolve()
        │
        ├── git branch --show-current       → current_branch
        ├── git rev-parse --short=8 HEAD    → head_commit
        ├── git status --porcelain=v1 -z    → staged/modified/untracked/deleted/renamed
        └── git merge-base HEAD <mainline>  → base_branch, base_commit
        │
        ▼
  GitContext  (immutable snapshot, frozen dataclass)
        │
        ├── IncrementalIndexCoordinator.compute_changes_git_aware()
        │     Uses staged + modified + untracked file lists
        │     to pre-filter the manifest scan.
        │     Falls back to full scan when git_context is None
        │     or working_tree_dirty is False.
        │
        ├── SimpleCache.make_key(... current_branch=, head_commit=, working_tree_dirty=)
        │     Branch/commit-aware cache keys prevent serving stale
        │     retrieval results across branch switches.
        │
        └── KnowledgeDocument / KnowledgeChunk
              branch_name, branch_scope, commit_sha, source_revision
              annotated on ingest when GitContext is available.
```

## Security Model

All Git subprocess calls go through `memory_engine.runtime.git.git_security`:

- `shell=False` — always pass args as a list, never a string
- Allowlist of safe subcommands: `rev-parse`, `branch`, `status`, `diff`, `merge-base`, `log`, `show`, `ls-files`, `for-each-ref`, `symbolic-ref`
- Blocklist of destructive commands raises `GitSecurityError` immediately
- Remote URLs are never fetched or returned
- Git user identity (name, email) is never collected
- All commands run inside the resolved project root
- Symlink resolution via `Path.resolve()` before passing to subprocess

## Graceful Degradation

`GitContextResolver.resolve()` never raises. If Git is unavailable:
- Returns `GitContext.unavailable()` — `git_available=False`, `is_repository=False`
- `IncrementalIndexCoordinator` falls back to full manifest scan
- Cache keys exclude branch/commit components (safe miss)
- Retrieval still works with base Phase 4 ranking

## Cache Invalidation

The cache key now includes `current_branch`, `head_commit`, and `working_tree_dirty`.

| Change                      | Effect                                  |
|-----------------------------|-----------------------------------------|
| Branch switch               | New cache key → cold start for branch   |
| New commit (HEAD advances)  | New cache key → cold start for commit   |
| Files modified (dirty tree) | `working_tree_dirty=True` → separate key|
| Clean tree (no changes)     | Stable key — cache hits as usual        |

## Configuration

```yaml
runtime:
  git_aware_sync: true            # Enable Git-aware sync
  check_git_status_on_retrieval: true  # Resolve GitContext on each retrieve call
  auto_incremental_sync: true
  optional_file_watcher: false    # File watcher (not enabled by default)
  watcher_debounce_ms: 1000
```
