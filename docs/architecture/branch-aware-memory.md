# Branch-Aware Memory

## Overview

Phase 9 adds branch-aware memory and knowledge retrieval. Memory nodes and knowledge documents are annotated with the Git branch they were written on, and the retrieval ranker re-weights results to prefer context that matches the agent's current working branch.

## Branch Metadata Model

Every `MemoryNode`, `EvidenceORM`, `MemoryCandidateORM`, `KnowledgeDocumentORM`, and `KnowledgeChunkORM` now has:

| Column                      | Type        | Meaning                                            |
|-----------------------------|-------------|---------------------------------------------------|
| `branch_name`               | VARCHAR(128)| Branch the node was written on, or NULL           |
| `branch_scope`              | VARCHAR(32) | `current_branch`, `mainline`, `global`, `historical` |
| `commit_sha`                | VARCHAR(64) | Short HEAD SHA at write time                      |
| `source_revision`           | VARCHAR(64) | Revision when the source file was last indexed    |
| `branch_promotion_eligible` | INTEGER     | 1 if ready for promotion to mainline              |

`KnowledgeDocumentORM` also has `valid_from_revision` and `valid_to_revision` for revision-range validity.

## Branch Scope Values

| Value             | Meaning                                               |
|-------------------|-------------------------------------------------------|
| `current_branch`  | Written on this branch, not yet merged                |
| `inherited_branch`| Merged from an ancestor branch                        |
| `mainline`        | On main/master/develop — applies to all branches      |
| `global`          | Not branch-specific (default for pre-Phase 9 data)   |
| `historical`      | Branch deleted; memory preserved for reference        |

## Branch-Aware Scoring Formula

When `current_branch` is provided in a retrieval request, `DeterministicRanker` applies a re-weighting:

```
final_score =
  0.35 × base_score               (Phase 4 formula: semantic+lexical+module+...)
+ 0.20 × branch_affinity          (current branch = 1.0, mainline = 0.5, global = 0.3)
+ 0.15 × revision_validity        (1.0 if no valid_to_revision, 0.0 if superseded)
+ 0.10 × working_tree_source_match (1.0 if node.source_path in modified_files)
+ 0.10 × source_revision_freshness (placeholder: 0.5 until full revision tracking)
+ 0.10 × branch_scope_priority     (current_branch=1.0, inherited=0.7, mainline=0.5, global=0.3)
```

When `current_branch` is `None` (e.g., Git unavailable), the formula degrades to `final_score = base_score`, preserving full backward compatibility.

## Branch Affinity Table

| Node branch_scope / branch_name       | current_branch signal |
|---------------------------------------|-----------------------|
| Same as current_branch                | 1.0                   |
| `inherited_branch`                    | 0.6                   |
| `mainline`                            | 0.5                   |
| `global` / no branch_name             | 0.3                   |
| Different unrelated branch            | 0.1                   |

## Memory Write Scoping

When `reflect_and_write` is called, the agent's current Git branch is resolved and stamped onto new memory candidates:

- Feature branch → `branch_scope = "current_branch"`
- Main/master/develop → `branch_scope = "mainline"`
- No Git → `branch_scope = "global"`

Mainline promotion (`branch_promotion_eligible = True`) requires explicit confirmation by default (`memory.mainline_promotion_requires_confirmation: true` in config).

## New RelationType Values (Phase 9)

| Value                        | Meaning                                          |
|------------------------------|--------------------------------------------------|
| `derived_from_branch`        | Memory derived from a specific branch's work     |
| `inherited_from_mainline`    | Memory inherited from mainline into a branch     |
| `promoted_to_mainline`       | Branch memory promoted to mainline               |
| `invalidated_by_branch_change` | Memory invalidated by a later branch commit    |
| `renamed_source`             | Source file was renamed                          |

## New MCP Resources (Phase 9)

| URI                                          | Content                              |
|----------------------------------------------|--------------------------------------|
| `memory://project/current/git-context`       | Current Git state (safe, no URLs)    |
| `memory://project/current/branch-memory-summary` | Memories by branch scope        |
| `memory://project/current/sync-status`       | Sync status and index freshness      |

## Configuration

```yaml
retrieval:
  branch_aware_ranking: true
  prefer_current_branch: true
  include_ancestor_branch_memory: true
  include_mainline_fallback: true
  include_historical_branch_memory_by_default: false

memory:
  branch_scope_on_feature_work: current_branch
  mainline_promotion_requires_confirmation: true
  branch_memory_retention_days: null   # retain indefinitely

privacy:
  expose_git_remote_url: false    # never expose remote URLs
  redact_git_identity: true       # never expose user name/email
```
