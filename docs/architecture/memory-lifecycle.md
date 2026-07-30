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
caller's selection behavior changes. As of Issue 6,
`UnifiedContextRetrievalService`'s `KnowledgeTraceEntry` conversion also
propagates this typed field (see below) — the follow-up flagged above is
now closed.

## Compact provenance (Issue 6)

Issues 1-5 each compute a piece of a memory's trustworthiness end-state
(source validity, constraint scope, provenance-based trust, verification-
evidence level, retrieval-time conflict status) but, before Issue 6, none
of it was surfaced compactly to the consuming agent — only as raw fields
scattered across the full `MemoryNode` dump. Issue 6 adds no new upstream
computation; it only formats and exposes what Issues 1-5 already decided.

`CompactProvenance` (`memory_engine/models/domain.py`) is a small, mostly-
optional model built once per candidate by
`memory_engine.skills.composer.build_provenance()` (pure formatting: reads
`node.status`, `node.validity_reason`, `constraint_scope.effective_scope()`,
`source_trust.effective_trust()`/`is_low_trust()`,
`verification_evidence.effective_evidence_level()`, and the ranker's
`score_breakdown` for a compact `matched_by` label — never recomputes any
of those decisions). It is attached to every `TraceEntry` built inside
`ContextComposer._fill_bucket()` (selected and excluded alike) and indexed
once more on `EnrichedContextPack.provenance` (keyed by memory id) so
`EnrichedContextPack.as_text()` can render compact extra lines per item
without needing the full retrieval trace. Because
`EnrichedContextPack.provenance` defaults to an empty dict, a pack built by
any pre-Issue-6 caller (composer-only tests, hand-built packs) renders
byte-identical `as_text()` output to before.

`RecallService.recall()` threads `current_branch` into `compose()` (for the
verification-evidence staleness check) and, after Issue 5's
`detect_conflicts()` runs, folds the outcome (`resolution_status`,
`len(alternatives)`, and the member's own `role` — exposed via
`ConflictInfo.own_role`, a new additive field alongside the pre-existing
`alternatives[*].role`) into the *same* `CompactProvenance` instance already
referenced by both the trace entry and the pack's provenance index — a
single mutation is visible from both places, no separate write path to
keep in sync.

The `fusion.py` gap called out above is closed by adding `conflict` and
`provenance` fields to `KnowledgeTraceEntry` (additive, default `None`) and
mapping them straight through in `_build_unified_pack`'s memory-trace loop.

Project-wide/envelope information (project id, a short, non-identifying
repository fingerprint) is deliberately **not** repeated per memory — it is
already implied once per response. `ProjectLocalStorage
.short_repository_fingerprint()` (`memory_engine/bootstrap/local_storage.py`)
truncates the already-computed, already-hashed Phase 14 `path_hash` to 12
hex characters (never the raw `canonical_path` or `remote_url_hash`) and is
surfaced once on `RetrievalMeta.repository_fingerprint` /
`RetrievalMeta.project_id` in the `retrieve_agent_context` MCP response.

No new database columns were added for Issue 6 — every field it surfaces
(`status`, `validity_reason`, `constraint_scope`, `trust_level`,
`evidence_level`, `branch_name`, `source_path`/`source_symbol`,
`commit_sha`) already existed on `MemoryNode` from Issues 1-4/Phase 9; the
only genuinely new stored bit is `ConflictInfo.own_role`, which is
retrieval-time-only (never persisted) exactly like the rest of `ConflictInfo`.

## Issues 1-6 pipeline — consolidated reference

The full mandated pipeline order,
`workspace/project validation → source validity → branch/scope eligibility
→ relevance gate → trust/authority checks → ranking → conflict handling →
composition → compact provenance`, is implemented end-to-end as follows:

| Stage | Implementation |
|---|---|
| Workspace/project validation | `memory_engine/mcp/tools.py:_validate_workspace()` (Phase 14), gates every MCP call before `RecallService` runs |
| Source validity | `memory_engine/services/source_validity.py` + `RecallService.recall()`'s bounded lazy-check block (Issue 1, `16370a6`-era commits / `9293e55a` incident fix) |
| Branch/scope eligibility | `memory_engine/services/constraint_scope.py` (`constraint_is_eligible`) called from `recall.py`'s `_passes_constraint_gate` (Issue 2) |
| Relevance gate | `memory_engine/skills/recall.py:_passes_relevance_gate()` (Phase 4, extended by Issue 2 for constraints) |
| Trust/authority checks | `memory_engine/services/source_trust.py` (`effective_trust`, `trust_meets_minimum`, `is_low_trust`) consumed by `constraint_scope.py` and `conflict_detection.py` (Issue 3) |
| Ranking | `memory_engine/skills/ranker.py:DeterministicRanker` (Phase 4/9/13 — lexical, semantic, branch-affinity, source-revision-freshness signals) |
| Conflict handling | `memory_engine/services/conflict_detection.py:detect_conflicts()`, wired into `RecallService.recall()` after composition (Issue 5) |
| Composition | `memory_engine/skills/composer.py:ContextComposer.compose()` (token-budgeted bucket fill + trace, Phase 4) |
| Compact provenance | `memory_engine/skills/composer.py:build_provenance()` + `memory_engine/models/domain.py:CompactProvenance`/`EnrichedContextPack.as_text()` + `memory_engine/knowledge/fusion.py`'s `KnowledgeTraceEntry` propagation (Issue 6) |

Verification evidence (Issue 4, `memory_engine/services/verification_evidence.py`)
is a cross-cutting signal consumed both at the trust/authority stage
(`adjusted_confidence()`) and at the compact-provenance stage
(`effective_evidence_level()` → `CompactProvenance.verification_level`),
rather than a single pipeline position of its own.
