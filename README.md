[English](README.md) | [中文](README.zh-CN.md)

# Memory Engine

**A local-first MCP runtime that gives coding agents persistent, evidence-backed project memory and grounded project knowledge across sessions.**

---

## Why it exists

Coding agents face a fundamental problem: every session starts cold.

- They forget project context between sessions.
- They repeatedly scan repositories to re-learn what modules do.
- They lose debugging lessons and historical decisions.
- Flat RAG cannot distinguish stable constraints, past incidents, architecture decisions, and raw code evidence.
- Even large context windows still require intelligent prioritization and token budgeting.

Memory Engine solves this by maintaining a structured, evidence-backed memory tree alongside an indexed project knowledge base — both local, both automatic, no infrastructure required.

---

## Core capabilities

| Capability | Details |
|---|---|
| **Persistent memory tree** | MemoryNode hierarchy: constraints, architecture, modules, decisions, incidents, procedures |
| **Evidence-backed memory** | Each node links to source Evidence entries (test output, code references, review notes) |
| **Candidate staging** | Reflection generates MemoryCandidates before promoting to the live tree |
| **Confidence-aware promotion** | create / update / merge / supersede / discard / needs_review |
| **Conflict detection** | High-risk areas (auth, schema, state-machine, retry) flagged for review |
| **Ancestor consolidation** | Parent node summaries auto-updated after each promotion |
| **Agent-native recall** | Intent-aware retrieval before coding tasks — no manual queries |
| **Progressive inspection** | Drill down into any memory node, its children, and linked evidence |
| **Automatic post-task reflection** | Agent reports outcome → system decides whether and how to retain knowledge |
| **Knowledge ingestion** | Markdown, code, ADR, test reports, runtime logs, git diffs |
| **Local FTS5 search** | SQLite FTS5 with porter tokenizer; no external search engine |
| **Optional vector retrieval** | InMemoryVectorIndex (ephemeral) or future persistent backends |
| **Lexical structured fallback** | Full retrieval without vector backend or Docker |
| **Unified ContextPack** | Memory + knowledge merged, deduplicated, token-budgeted |
| **Retrieval traceability** | Per-signal score breakdown in every response |
| **Local-first privacy** | All data stays inside `.memory-engine/`; no telemetry, no cloud calls |
| **Python MCP server** | stdio transport; no TypeScript, no Docker, no external daemon |
| **Zero-touch bootstrap** | Auto-initializes on first MCP connection |
| **Incremental indexing** | JSON manifest; only changed files re-indexed on subsequent runs |

---

## Quick Start

### Prerequisite: install `uv` once

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. Clone Memory Engine

```bash
git clone https://github.com/your-org/memory-engine
```

### 2. Copy an MCP configuration block

**Option A — explicit project root:**

```json
{
  "mcpServers": {
    "memory-engine": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/memory-engine",
        "memory-engine-mcp",
        "--project-root",
        "/absolute/path/to/your-project"
      ]
    }
  }
}
```

**Option B — project root via environment variable:**

```json
{
  "mcpServers": {
    "memory-engine": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/memory-engine",
        "memory-engine-mcp"
      ],
      "env": {
        "MEMORY_ENGINE_PROJECT_ROOT": "/absolute/path/to/your-project"
      }
    }
  }
}
```

> **Note:** Replace all `/absolute/path/to/...` with real paths on your machine.
> Config file location and workspace-variable support differ by client:
> - **Cursor:** `.cursor/mcp.json` or global Cursor MCP settings
> - **Claude Code:** `~/.claude.json` or project-level config
> - Consult your client's MCP documentation for exact placement.

### 3. Open your target project and start coding

That's it. Memory Engine starts automatically and handles everything else.

---

## What happens automatically

```
User opens project
     │
     ▼
MCP client starts memory-engine-mcp via stdio
     │
     ▼
Project root resolved (.git / pyproject.toml / package.json marker)
     │
     ▼
.memory-engine/ created (if first use)
     │
     ▼
README, ADRs, architecture docs, constraints indexed first
     │
     ▼
Broader source files indexed incrementally in background
     │
     ▼
Agent starts non-trivial coding task
     │
     ▼  [automatic]
retrieve_agent_context called
     │   → relevant constraints, incidents, decisions, procedures, source refs
     │
     ▼
Agent implements and validates
     │
     ▼  [automatic, on success]
reflect_and_write called
     │   → system evaluates retention gates
     │   → creates MemoryCandidates if worthy
     │   → promotes to memory tree
     │   → consolidates ancestor summaries
     │
     ▼
memory_status shows updated counts
```

---

## Architecture

```mermaid
graph TD
    A[User / Coding Agent] --> B[MCP Client]
    B -->|stdio| C[Python MCP Server]
    C --> D[Agent Skills]
    C --> E[Service Layer]
    C --> F[Knowledge Layer]
    D -->|recall / inspect / reflect| E
    E -->|memory lifecycle| G[(Memory Tree\nSQLite)]
    F -->|ingest / search| H[(Knowledge Base\nSQLite + FTS5)]
    F --> I[Optional Vector Backend]
    E --> G
    F --> H
    C --> J[Bootstrap & Incremental Index]
    J --> K[.memory-engine/\nproject-local storage]
    G --> K
    H --> K
```

---

## Agent calling chain

```mermaid
sequenceDiagram
    participant Agent
    participant MCP as MCP Server
    participant Skills as Agent Skills
    participant Services as Service Layer
    participant DB as SQLite / FTS5

    Agent->>MCP: retrieve_agent_context(task, files, symbols)
    MCP->>Skills: QueryAnalyzer.analyze()
    Skills->>DB: MemoryNode recall (intent-weighted SQL)
    Skills->>DB: KnowledgeSearch (FTS5 + vector RRF)
    Skills->>Skills: Rank, compose, dedup, token-trim
    MCP-->>Agent: ContextPack (memory + knowledge + trace)

    Note over Agent: implements and validates

    Agent->>MCP: reflect_and_write(task, outcome, verification_status)
    MCP->>Skills: ReflectionSkill.analyze() — gate check
    Skills->>Services: PostTaskService.reflect_and_write()
    Services->>Services: PromotionService.promote()
    Services->>Services: ConsolidationService.update_ancestors()
    Services->>DB: persist MemoryNode updates
    MCP-->>Agent: {outcome: "persisted", candidates_promoted: 2}
```

---

## Memory lifecycle

```
Task result
    │
    ▼
ReflectionSkill.analyze()
    │  gates: outcome ≠ failed/reverted, verification_status, confidence ≥ threshold,
    │         summary word count, known-trivial patterns
    │
    ├─ skip → return {skip_reason}
    │
    └─ pass ▼
    │
MemoryCandidate generation
    ├─ constraint     (importance 0.92)
    ├─ procedure      (importance 0.72)
    ├─ incident/debug (importance 0.85)
    ├─ module         (importance 0.62)
    └─ decision       (importance 0.82)
    │
    ▼
PromotionService.promote()
    ├─ create     — new node
    ├─ update     — same title, content refreshed
    ├─ merge      — near-duplicate (Jaccard ≥ 0.80)
    ├─ supersede  — existing node confirmed wrong
    ├─ discard    — low value / already known
    └─ needs_review — conflicts with high-confidence existing node
    │
    ▼
ConsolidationService.update_ancestors()
    │  parent.summary = concat(children.summaries)
    ▼
cache invalidated + memory_revision bumped
```

### Node statuses

| Status | Meaning |
|---|---|
| `candidate` | Staged, pending promotion decision |
| `active` | Live, returned in recall |
| `stale` | Outdated; preserved for history |
| `superseded` | Replaced by newer node |
| `needs_review` | Flagged conflict; human review recommended |

---

## Knowledge lifecycle

```
Documents / code / ADRs / tests / logs / diffs
    │
    ▼
redact()  ← 8 patterns: API keys, tokens, passwords, private keys,
           │             connection strings, JWTs, AWS keys, Slack tokens
    ▼
SHA-256 content hash → dedup check
    │
    ▼
Source-type chunker
    ├─ Markdown    → heading-based sections (≤1200 tokens)
    ├─ Code        → class/function blocks   (≤1000 tokens)
    ├─ Test report → result windows
    ├─ Log         → sliding windows         (≤600 tokens)
    └─ Diff/Patch  → hunk-based chunks
    │
    ▼
KnowledgeDocument + KnowledgeChunk (SQLite)
    │
    ├─ FTS5 insert    (lexical — always available)
    └─ Vector upsert  (optional — InMemoryVectorIndex or Qdrant)
    │
    ▼
hybrid retrieval → RRF fusion → source-quality ranking
    │
    ▼
UnifiedContextPack (40% of token budget)
```

---

## Directory structure

```
memory_engine/
├── main.py                  ← FastAPI app (dev / direct API use)
├── cli.py                   ← Debug CLI
├── config.py                ← Pydantic Settings
│
├── agent/                   ← Stage 8 namespace (re-exports)
│   ├── skills/              → memory_engine.skills
│   ├── policies/            → reflection gate constants
│   └── contracts/           → agent I/O domain models
│
├── skills/                  ← agent-facing behaviors (recall, inspect, reflect)
├── services/                ← domain orchestration (promotion, consolidation)
├── knowledge/               ← ingestion, chunking, FTS5, vector, search, fusion, cache
├── repositories/            ← persistence abstraction (memory_node, candidate, evidence)
├── models/                  ← Pydantic domain + SQLAlchemy ORM
│
├── bootstrap/               ← local runtime (project_root, storage, security, state)
├── runtime/                 ← Stage 8 namespace (re-exports bootstrap + cache + config)
│
├── mcp/                     ← MCP adapter (tools, resources, server, project_context)
├── api/                     ← FastAPI routes
└── db/                      ← SQLite session + init

docs/
├── architecture/            ← system-overview, memory-lifecycle, knowledge-pipeline,
│                               retrieval-pipeline, mcp-integration, local-runtime
└── guides/                  ← quickstart, configuration, privacy-and-security

tests/
├── test_phase4.py – test_phase7.py   ← phase integration tests
└── test_*.py                          ← unit and component tests
```

---

## MCP tools

| Tool | Purpose |
|---|---|
| `retrieve_agent_context` | Retrieve memory + knowledge before a coding task |
| `inspect_memory` | Drill into a MemoryNode, its children, and evidence |
| `inspect_knowledge` | Inspect a KnowledgeChunk or source file range (redacted) |
| `reflect_and_write` | Report validated work to the reflection pipeline |
| `memory_status` | Project health, retrieval mode, index counts, revisions |
| `refresh_project_knowledge` | Trigger incremental rescan (explicit use only) |

## MCP resources

| Resource | Content |
|---|---|
| `memory://project/current/constraints` | Active project constraints |
| `memory://project/current/architecture` | Architecture and module summaries |
| `memory://project/current/status` | Bootstrap state, retrieval mode, health |
| `memory://project/current/recent-incidents` | Recent debug incidents |
| `memory://project/current/memory-tree-summary` | Memory tree outline |
| `memory://project/current/agent-policy` | Generated agent policy |

---

## Local storage

```
your-project/.memory-engine/
├── config.yaml              ← edit to customize; never overwritten
├── project_state.json       ← bootstrap status, revisions
├── memory.db                ← all data (memories, knowledge, candidates)
├── indexes/manifests/       ← incremental indexing file manifest
├── generated/
│   └── AGENT_MEMORY_POLICY.md
├── bootstrap/bootstrap_report.json
├── constraints.md           ← human-authored; safe to commit
├── team-rules.md            ← human-authored; safe to commit
└── decisions.md             ← human-authored; safe to commit
```

Add `.memory-engine/` to `.gitignore` (generated hint on first bootstrap).
The three human-authored `.md` files may optionally be committed.

**Reset:** `rm -rf your-project/.memory-engine/`

---

## Human-authored seed knowledge

Create these files to provide stable project knowledge that cannot be safely
inferred from code alone:

**`.memory-engine/constraints.md`**
```markdown
# Project Constraints

## Auth
Do not bypass JWT validation. All routes require Bearer token.

## Database
Never use raw SQL. SQLAlchemy ORM only.

## Scheduler
Terminal task states (COMPLETED, FAILED, CANCELLED) are immutable.
```

**`.memory-engine/team-rules.md`**
```markdown
# Team Rules

- PRs require 2 approvals before merge
- All public APIs must have OpenAPI documentation
- Log structured JSON only (no print statements in production code)
```

These files are indexed as high-priority knowledge on bootstrap and returned in
context before relevant tasks.

---

## Retrieval modes

### Default local mode: `lexical_structured_fallback`

Active when no persistent vector backend is available (default for local use).

Signals used for ranking:
- SQLite FTS5 lexical match (BM25)
- Module-path overlap with current task files
- Symbol overlap with current task symbols
- Memory tree proximity
- Node importance and confidence
- Freshness (recency weighting)
- Project-scoped TTL cache

### Enhanced mode: `hybrid_lexical_vector`

Active when a persistent vector backend is healthy.
Adds cosine similarity over chunk embeddings via RRF fusion.

**Vector retrieval is optional.** The default local mode works without
Qdrant, Docker, or any external service.

---

## Privacy and security

- **Local-only:** all data stays in `.memory-engine/`; nothing leaves your machine
- **No telemetry:** no usage data sent anywhere
- **No cloud embedding:** no external API calls by default
- **No Docker:** not required for any feature
- **Path boundaries:** all file reads restricted to resolved project root
- **Symlink protection:** links escaping project root rejected
- **Secret redaction:** runs before persistence and before MCP output
- **Default exclusions:** `.env`, `secrets/`, `*.pem`, `*.key`, `node_modules/`, `.git/`, binary files, files over 5 MB
- **No auto Git commits:** never
- **No writes outside `.memory-engine/`:** guaranteed

---

## Configuration

Generated at `.memory-engine/config.yaml` on first bootstrap:

```yaml
project:
  name: auto
  root_path: auto

runtime:
  auto_bootstrap: true
  auto_recall: true
  auto_reflect: true
  auto_index_on_start: true
  incremental_indexing: true

privacy:
  mode: local
  redact_secrets: true
  allow_network_embedding: false

knowledge:
  include:
    - README.md
    - docs/**
    - src/**
    - app/**
    - lib/**
    - tests/**
  exclude:
    - node_modules/**
    - .git/**
    - .venv/**
    - dist/**
    - build/**
    - .env
    - secrets/**
  max_file_size_mb: 5

retrieval:
  default_token_budget: 6000
  cache_enabled: true
  vector_backend: auto
  allow_degraded_fallback: true
```

User edits are preserved on re-bootstrap.

---

## Demo scenario

**Scheduler project. Task:** Add exponential retry backoff without breaking terminal task state semantics.

1. Agent calls `retrieve_agent_context`:

```json
{
  "constraints": [
    {
      "title": "Terminal State Immutability",
      "summary": "COMPLETED, FAILED, CANCELLED are terminal states. Any operation that transitions out of a terminal state is a critical bug.",
      "importance": 0.95
    }
  ],
  "incidents": [
    {
      "title": "Retry Loop Re-entered Terminal Task",
      "summary": "In v0.8.2, a retry race condition re-entered a COMPLETED task. Root cause: retry check did not verify terminal status before re-queuing.",
      "importance": 0.88
    }
  ],
  "knowledge_chunks": [
    {
      "source_path": "docs/adr/003-retry-policy.md",
      "preview": "Decision: use exponential backoff with jitter. Max 5 retries..."
    }
  ],
  "retrieval_trace": [...],
  "meta": {
    "retrieval_mode": "lexical_structured_fallback",
    "vector_backend": "ephemeral",
    "warnings": ["Semantic vector retrieval is unavailable..."]
  }
}
```

2. Agent implements retry logic with terminal-state guard.
3. Tests pass. Agent calls `reflect_and_write`:

```json
{
  "outcome": "persisted",
  "candidates_promoted": 2,
  "consolidation_notes": ["Parent 'Scheduler Core' summary updated"]
}
```

---

## Debug CLI

For maintainers, demos, and troubleshooting only. Not the normal workflow.

```bash
memory-engine debug status --project-root /path/to/project
memory-engine debug bootstrap --project-root /path/to/project
memory-engine debug index --project-root /path/to/project
memory-engine debug recall "add retry backoff" --project-root /path/to/project
memory-engine debug inspect <node-id>
memory-engine debug reset-project --project-root /path/to/project
```

---

## Testing

```bash
# Run all tests
pytest -v

# Run focused
pytest tests/test_phase7.py -v
pytest tests/test_phase6.py -v
pytest -k "recall" -v
```

215 tests currently passing. All deterministic. No external services required.

---

## Limitations and future work

- **Persistent local vector backend** — current InMemoryVectorIndex does not survive process restarts
- **Optional Qdrant backend** — interface exists; client not installed by default
- **PyPI publishing** — `pip install memory-engine-mcp` not yet available
- **Binary packaging** — no binary installer yet
- **Client-specific installers** — no Cursor/VS Code extension yet
- **Streamable HTTP remote mode** — stdio only; no team-shared HTTP transport yet
- **Team-shared memory** — each project has isolated local storage; no shared team memory yet
- **Authentication and permissions** — no per-user or per-team access control yet
- **Richer code parsing** — chunking is line-range based; AST-aware parsing is future work
- **Larger repository benchmarks** — not yet validated at monorepo scale

---

## Contributing

1. Read [`docs/architecture/system-overview.md`](docs/architecture/system-overview.md) first.
2. Preserve service-layer boundaries: MCP/API layers stay thin.
3. Keep business logic in `skills/`, `services/`, `knowledge/`.
4. New knowledge source types → `knowledge/chunkers.py` + new `SourceType` enum value.
5. New MCP tools → `mcp/tools.py`, thin wrapper only; delegate to services.
6. New domain model → `models/domain.py` or `models/knowledge_domain.py`.
7. Add tests for all new behavior.
8. Never weaken project-root security boundaries.
9. Run `pytest -v` and confirm all tests pass before opening a PR.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -v

# Start FastAPI service (dev / direct API use)
uvicorn memory_engine.main:app --reload
# API docs at http://localhost:8000/docs

# Run MCP server directly
uv run memory-engine-mcp --project-root /path/to/project --log-level DEBUG
```
