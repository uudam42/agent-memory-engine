# Memory Retention & Compaction (Phase 11)

## Overview

Phase 11 extends the memory lifecycle beyond candidate staging and promotion into
a full **retention governance** system. Active memory no longer grows unboundedly;
instead, memories transition through a well-defined lifecycle with explicit audit
trails and no destructive physical deletion.

---

## Lifecycle State Machine

```
MemoryCandidate (pending)
      │
      ├── promotion → MemoryNode (active)
      │                    │
      │                    ├── remains useful → active
      │                    ├── outdated → stale / superseded
      │                    ├── historically useful → archived
      │                    └── related group → compacted summary
      │                                           │
      │                                           └── links to source memories / evidence
      │
      └── exceeds retention window → expired (candidate)
```

### State definitions

| State | Description | Default retrieval |
|---|---|---|
| `candidate` | Pending promotion decision | Never retrieved as active memory |
| `active` | Current usable memory | Eligible for normal retrieval |
| `stale` | Potentially outdated | Strongly penalized or excluded |
| `superseded` | Replaced by newer verified memory | Excluded by default |
| `needs_review` | Conflict or uncertain validity | Excluded by default |
| `archived` | Historical or audit-only value | Excluded by default |
| `compacted` | Stable synthesis of source memories | Eligible with lineage |
| `expired` | Candidate never promoted, past window | Excluded |

---

## Candidate Expiry

Candidates unpromoted beyond `candidate_expiry_days` (default: 30) are transitioned
to `expired` status:

- `status = "expired"`
- `expiry_reason` records the expiry cause
- `expires_at` records the timestamp
- No physical deletion; candidates remain auditable

---

## Archival Rules

Active memories are eligible for archival when:

1. **Stale memories** unchanged for >`stale_archive_days` (default: 180 days)
2. **Superseded memories** inactive for >`inactive_archive_days` (default: 120 days)
3. **Branch-local memories** when their branch is no longer active (configurable window)

### Protected types (never auto-archived)

```
constraint
security_rule
architecture
decision
```

Protected types are excluded from all automatic lifecycle transitions. Manual
archival remains possible via `memory retention restore <id>`.

---

## Compaction

Compaction combines multiple related lower-level memories into a stable summary
node. Source memories are preserved and linked.

### Eligibility criteria (all must be satisfied)

- Same project
- Same parent memory scope (`parent_id`)
- Same memory type (`kind`)
- Status = `active`
- Kind not in protected types
- Age ≥ `compaction_min_age_days` (default: 14)
- Group size ≥ `compaction_min_related_memories` (default: 3)

### Output structure

A compacted node contains:
- `title`: `[Compacted] <kind> group: <source titles>`
- `summary`: Combined summary of all source memories
- `status = "compacted"`
- Source memories linked via `memory_relations.relation_type = "compaction_source"`
- Source `compacted_into_id` → points to compacted node

### Must not compact

- Security constraints
- Authorization rules
- Conflicting architecture decisions
- High-confidence incidents under review
- Protected memory types

---

## Branch Lifecycle Cleanup

Branch-local memories (`branch_scope = "current_branch"`) for branches that are
no longer active can be archived after a configurable window
(`archive_merged_branch_memories_after_days`, default: 30).

**Safety rules:**
- Read-only Git inspection only — no remote calls, no remote URL exposure
- Promoted memories (`branch_promotion_eligible = 1`) are always preserved
- Mainline/global-scoped memory is never auto-archived
- Current-branch memory is never auto-archived by age alone

---

## Retrieval Integration

| Status | Default retrieval |
|---|---|
| `active`, `compacted` | Included |
| `stale` | Strongly penalized |
| `superseded`, `needs_review`, `archived` | Excluded |

Compacted memories are preferred over returning all source memories simultaneously
unless the query explicitly requests deep evidence expansion.

---

## Configuration (config.yaml)

```yaml
memory_retention:
  enabled: true
  candidate_expiry_days: 30
  inactive_archive_days: 120
  stale_archive_days: 180
  compaction:
    enabled: true
    min_related_memories: 3
    min_age_days: 14
    require_same_parent_scope: true
    require_same_memory_type: true
    preserve_original_evidence: true
    auto_archive_source_memories: true
  branch_cleanup:
    enabled: true
    archive_deleted_branch_memories: true
    archive_merged_branch_memories_after_days: 30
    preserve_promoted_mainline_knowledge: true
  protected_memory_types:
    - constraint
    - security_rule
    - architecture
    - decision
```

---

## CLI Commands

```bash
# Diagnostics (read-only)
memory retention status <project>
memory retention report <project>

# Dry-run (default — no mutations)
memory retention run <project> --dry-run
memory retention run <project>             # also dry-run by default

# Apply changes
memory retention run <project> --no-dry-run

# Restore an archived memory
memory retention restore <project> <memory-id>
```

---

## MCP Resources

```
memory://project/current/retention-status   — lifecycle count diagnostics
memory://project/current/compaction-report  — compaction group candidates
```

---

## Safety Boundaries

- Default retention runs are always `--dry-run` unless explicitly overridden
- No automatic physical deletion — only lifecycle state transitions
- No compaction without full source lineage
- No compaction of protected memory types
- No cross-project or cross-branch compaction
- All transitions emit structured reports
- Source evidence is always preserved through compaction

---

## Database Schema (Phase 11 additions)

### `memory_nodes` new columns

| Column | Type | Description |
|---|---|---|
| `archived_at` | DATETIME | When archived (NULL if not archived) |
| `archived_reason` | VARCHAR(512) | Why archived |
| `compacted_into_id` | VARCHAR(36) | ID of compacted node (if compacted source) |
| `last_retrieved_at` | DATETIME | Last time retrieved in a context query |
| `retrieval_count` | INTEGER | Lifetime retrieval count |

### `memory_candidates` new columns

| Column | Type | Description |
|---|---|---|
| `expires_at` | DATETIME | When expired (NULL if not expired) |
| `expiry_reason` | VARCHAR(512) | Why expired |

### `memory_relations` new relation_type

| Value | Meaning |
|---|---|
| `compaction_source` | Compacted node → source memory link |

---

## Migration

Phase 11 schema changes are applied via `apply_schema_migrations()` in
`memory_engine/db/init_db.py` using `ALTER TABLE ADD COLUMN`. This is:

- **Safe**: SQLite ADD COLUMN is non-destructive
- **Idempotent**: duplicate column errors are silently discarded
- **Backward compatible**: existing databases continue to work unchanged
- **Zero downtime**: no table rebuilds, no data loss, no forced reindex

Existing active memory records have `archived_at = NULL`,
`retrieval_count = 0`, and `compacted_into_id = NULL` — they continue
to behave as active memory normally.
