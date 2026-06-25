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

Used as a cache invalidation signal. Cache keys include `memory_revision`.
