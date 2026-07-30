# Memory Lifecycle

## MemoryNode statuses

```
                    ┌─────────────────┐
task completed      │   candidate     │  (MemoryCandidate, not yet MemoryNode)
─────────────────►  │   (staging)     │
                    └────────┬────────┘
                             │ PromotionService.promote()
                    ┌────────▼────────┐
                    │    active       │  ◄── default live state
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───┐  ┌───────▼────┐  ┌────▼──────────┐
     │   stale    │  │ superseded │  │  needs_review  │
     └────────────┘  └────────────┘  └───────────────┘
```

## Promotion outcomes

| Outcome | When |
|---|---|
| `create` | No existing node; new knowledge |
| `update` | Existing node with same title; content update |
| `merge` | Near-duplicate detected; content merged |
| `supersede` | Existing node confirmed wrong or obsolete |
| `discard` | Low value; already known; too vague |
| `needs_review` | Conflicting with high-confidence existing node |

## Deduplication logic

`DeduplicationService` computes semantic overlap using:
- normalized title similarity (Jaccard token overlap)
- module_path intersection
- tag intersection

Threshold: `DEDUP_THRESHOLD = 0.80` (Jaccard ≥ 0.80 → merge candidate)

## Conflict detection

`ConflictService` detects when a candidate's summary contradicts an existing
active node covering the same module path and title root.

High-risk patterns (regex `_HIGH_RISK_RE` in `reflection.py`):
- state-machine, auth, schema, migration, retry, backoff, timeout, idempotent

## Consolidation

After promotion, `ConsolidationService.update_ancestors()` regenerates the
`summary` field of all ancestor nodes by concatenating direct children summaries.

This keeps high-level architecture nodes current without manual maintenance.

## Stale management

`MemoryNode.status = "stale"` is set when:
- `PromotionService.mark_stale()` is called explicitly
- A superseding candidate is promoted on top of an existing node

Stale nodes are excluded from default recall but preserved for historical trace.

## Revision tracking

`project_state.json` tracks `memory_revision` (int). Incremented on every:
- candidate promotion
- stale marking
- supersession
- a source-validity transition discovered during retrieval (see below)

Used as a cache invalidation signal. Cache keys include `memory_revision`.

## Source-validity lifecycle (automatic stale detection)

Added in Phase 15 (`f8db7eb`, `SourceValidityService`) and closed out in the
follow-up phase described here. Two additional `MemoryStatus` values exist
alongside the ones above:

```
        active ──(source file deleted)──────────► invalidated
        active ──(source hash/symbol/commit drift)──► needs_revalidation
needs_revalidation ──(explicit revalidate(), source healthy)──► active
   invalidated ──(explicit revalidate(), source healthy)──► active
```

Transitions are **never** automatic in the `active`-restoring direction —
only `SourceValidityService.revalidate()`, called explicitly, can move a
node back to `active`. Nothing is ever deleted; `previous_status`,
`validity_reason`, and `validity_checked_at` on `MemoryNodeORM` form a
permanent audit trail (`MemoryNodeRepository.set_validity()`).

### Which memories are source-backed

Only nodes with `source_path` set are ever touched. `source_path` is
populated automatically by `ReflectionSkill` (Task 2) when, and only when,
a single touched file unambiguously identifies the origin of a `debug`
(incident) or `module` candidate — the "strongly source-backed" kinds
(Task 7). `constraint`, `procedure`, and `decision` candidates are treated
as human-confirmed / repository-level policy and are **never** auto-bound
to a source file, even when exactly one file was touched, so they cannot
become non-authoritative merely because that file's hash drifted. Legacy
memories (no `source_path`) are always a no-op for validity checks —
"provenance-unknown" is represented by the field's absence, not a separate
flag.

### Hash / symbol / commit semantics

- `source_hash` — full-file sha256, computed once via
  `compute_source_hash()` at promotion time (bounded/streamed read, rejects
  symlink/path escape via `resolve_safe`, never hashes directories or
  missing files). Coarse: any byte-level change anywhere in the file
  changes the hash. No symbol-range or normalized-text hash exists in this
  codebase to do better; this is the documented limitation.
- `source_symbol` — optional, set only when exactly one touched symbol
  accompanies exactly one touched file. Checked with a regex scan for
  `def`/`class`/`async def` declarations, **Python only**
  (`symbol_exists_in_file`); other languages fall back to file/hash-level
  checking only, and this is an explicitly documented, not silently faked,
  limitation.
- `commit_sha` (reused from the Phase 9 branch-aware field) — checked via
  read-only `git rev-parse --verify` + `git merge-base --is-ancestor`
  (`check_commit_reachable`). Any Git error, missing repository, or
  malformed SHA yields "unknown" (no-op) — never fabricated validity.
  Reachability results are cached for the lifetime of one
  `SourceValidityService` instance (i.e. one recall/retrieve call).

### Where validation runs

- **Cache miss** — `RecallService.recall()` validates every loaded
  candidate that carries `source_path` (bounded to the project's node
  count, no repository-wide file scan) before scoring/composing.
- **Cache hit** — `UnifiedContextRetrievalService._revalidate_cached_memories()`
  re-checks only the memory IDs referenced as "selected" in the *cached*
  pack's retrieval trace (bounded — typically a handful) before returning
  it. If any transitioned, the specific cache key is invalidated and the
  request falls through to a full recall. This closes the gap where a
  cache entry populated before a file deletion/edit could otherwise be
  served indefinitely (until TTL expiry) without ever re-checking.
- Either path fires `revision_hook` (wired from `mcp/tools.py` to
  `ProjectState.bump_memory()`) exactly once per **persisted** transition —
  never on a no-op check — so the next retrieval call's cache key also
  reflects the change.

### project_root threading

`ProjectContext.project_root` (already validated by the Phase 14 workspace
handshake) is the only project root ever passed into
`UnifiedContextRetrievalService` / `RecallService` / `PromotionService` from
the MCP tool layer (`mcp/tools.py`). Direct unit-test construction with
`project_root=None` remains fully supported and performs zero filesystem
access — this is the default for every pre-existing caller.

## Explicit conflict detection (Issue 5)

Branch-affinity ranking (Phase 9) changes *which* memory a task sees first
but never tells the agent that two otherwise-qualifying memories actively
disagree (e.g. `main`: "use REST for service communication" vs.
`feature/grpc`: "use gRPC for service communication"). Picking one by score
alone hides that material conflict.

`memory_engine/services/conflict_detection.py`'s `detect_conflicts()` runs
inside `RecallService.recall()`, **after** `ContextComposer.compose()` — i.e.
only over candidates that already survived source-validity (Issue 1),
constraint-scope eligibility (Issue 2), and the relevance gate, and that
composer has actually selected into the context pack. Within that already
small set, it additionally excludes:

- non-authoritative statuses (`stale`, `superseded`, `archived`,
  `needs_revalidation`, `invalidated`, `needs_review`);
- authoritative-kind (`constraint`/`architecture`/`decision`) nodes that
  fail Issue 3's `trust_meets_minimum` threshold.

Members are grouped by, in priority order: an explicit stored
`contradicts`/`supersedes` `MemoryRelation` (bypasses branch bounding — a
`supersedes` edge instead **drops** the superseded side from membership
entirely, defensively, even if its `status` transition hasn't landed yet);
otherwise a shared `source_symbol`, `source_path`, `module_path`, or (for
`decision`-kind nodes with none of those) an exact-match normalized-title
key. Identity-based grouping is bounded to nodes that are unscoped/global,
mainline-ish (`branch_scope` in `{global, mainline, inherited_branch}`), or
on the request's own `current_branch` — a memory that only exists on some
other, unrelated branch is never pulled into a conflict by identity alone.

Resolution:

- exactly one current-branch member + one-or-more mainline/global members
  → `current_branch_preferred`, the current-branch member is
  `role="preferred"`, the rest `role="historical"`;
- anything else (no `current_branch`, two members on the same branch, an
  unrelated-branch member reachable only via a relation, etc.) →
  `unresolved` — every member is `role="unresolved_peer"`. Score is never
  consulted to break this tie.

The result is a fully retrieval-time, in-memory computation — no new table,
no persisted relation rows, `conflict_group_id` is a stable hash of the
sorted member ids (deterministic across repeated calls). It is attached as
`TraceEntry.conflict` on the affected "selected" trace entries;
`EnrichedContextPack`'s node lists are untouched, so no pre-existing
caller's selection behavior changes. `UnifiedContextRetrievalService`'s
`KnowledgeTraceEntry` conversion does not yet propagate this typed field —
documented as a follow-up for the still-pending "compact provenance" work.
