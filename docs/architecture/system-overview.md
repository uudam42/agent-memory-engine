# System Overview

Memory Engine is a local-first MCP runtime that gives coding agents persistent,
evidence-backed project memory and grounded project knowledge across sessions.

---

## High-level architecture

```
Coding Agent / IDE
     │
     │  MCP stdio
     ▼
Python MCP Server  (memory_engine.mcp)
     │
     ├──► Agent Skills  (memory_engine.skills)
     │         ├─ RecallService
     │         ├─ InspectService
     │         ├─ ReflectionSkill
     │         ├─ DeterministicQueryAnalyzer
     │         ├─ DeterministicRanker
     │         └─ ContextComposer
     │
     ├──► Service Layer  (memory_engine.services)
     │         ├─ PostTaskService
     │         ├─ PromotionService
     │         ├─ ConsolidationService
     │         └─ MemoryService
     │
     ├──► Knowledge Layer  (memory_engine.knowledge)
     │         ├─ KnowledgeIngestionService
     │         ├─ KnowledgeSearchService
     │         ├─ UnifiedContextRetrievalService
     │         ├─ FTS5 lexical index
     │         ├─ InMemoryVectorIndex (ephemeral)
     │         └─ TTL cache (project-scoped)
     │
     ├──► Repository Layer  (memory_engine.repositories)
     │
     ├──► Models
     │         ├─ Domain  (memory_engine.models.domain, knowledge_domain)
     │         └─ ORM     (memory_engine.models.orm, knowledge_orm)
     │
     └──► Runtime / Bootstrap  (memory_engine.bootstrap)
               ├─ ProjectBootstrapService
               ├─ IncrementalIndexCoordinator
               ├─ ProjectLocalStorage  (.memory-engine/)
               ├─ ProjectStateManager
               └─ Security boundary enforcement
```

---

## Service boundaries

| Layer | Responsibility | May call |
|---|---|---|
| `mcp/` | Input validation, project-context resolution, output serialization | `skills/`, `services/`, `knowledge/` |
| `skills/` | Agent-facing autonomous behaviors | `repositories/`, `services/`, `knowledge/` |
| `services/` | Domain orchestration and lifecycle | `repositories/`, `models/` |
| `knowledge/` | Ingestion, indexing, retrieval, cache | `models/`, `repositories/`, `db/` |
| `repositories/` | Persistence abstraction | `models/orm`, `db/` |
| `bootstrap/` | Process startup, storage layout, security | `knowledge/`, `db/`, `models/` |
| `api/` | FastAPI routes (dev/direct use) | `skills/`, `services/`, `knowledge/` |

**Rule:** MCP and API layers must not contain domain logic. They call services and
return structured responses.

---

## Data flow: before a coding task

```
1. Agent calls retrieve_agent_context(task, files, symbols)
2. MCP tool resolves ProjectContext (bootstraps if first use)
3. QueryAnalyzer parses task → TaskIntent, module_paths, symbols
4. RecallService queries MemoryNodeORM with intent-weighted scoring
5. KnowledgeSearchService runs RRF(FTS5 + InMemoryVector)
6. UnifiedContextRetrievalService merges results, deduplicates, applies token budget
7. Returns ContextPack with retrieval trace
```

## Data flow: after a coding task

```
1. Agent calls reflect_and_write(task, outcome, verification_status, changed_files)
2. ReflectionSkill evaluates gates (outcome, confidence, word-count, verification)
3. If passes → generates MemoryCandidates (constraint, procedure, incident, decision)
4. CandidateRepository.create() → PromotionService.promote()
5. Promotion: create / update / merge / supersede / needs_review / discard
6. ConsolidationService updates ancestor summaries
7. Cache invalidated; project_state.json revisions bumped
```

---

## Memory vs Knowledge

| Concept | Memory (MemoryNode) | Knowledge (KnowledgeChunk) |
|---|---|---|
| What | Compressed engineering understanding | Raw source-grounded content |
| Created by | ReflectionSkill / PostTaskService | KnowledgeIngestionService |
| Lifecycle | candidate → active → stale → superseded | indexed → stale |
| Retrieved by | RecallService (structured scoring) | KnowledgeSearchService (FTS5 + vector RRF) |
| Token weight | ~200–500 tokens (compressed) | ~800–1200 tokens (source content) |
| Persists across sessions | Yes (SQLite) | Yes (SQLite) |

Both are returned in a single `UnifiedContextPack` with a shared token budget.
