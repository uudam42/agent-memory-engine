# MCP Integration

## Transport

Memory Engine uses the Python MCP SDK with stdio transport.

The MCP client (Cursor, Claude Code, or another compatible agent) launches
the Python process directly. No HTTP server, no Docker, no external daemon.

```
MCP Client
   │
   │  stdin/stdout (JSON-RPC over stdio)
   ▼
memory-engine-mcp process  (memory_engine.mcp.server:main)
   │
   ▼
FastMCP("memory-engine")
   ├── 6 tools
   └── 6 resources
```

## Entry point

```bash
uv run memory-engine-mcp --project-root /path/to/project
```

Defined in `pyproject.toml`:
```toml
[project.scripts]
memory-engine-mcp = "memory_engine.mcp.server:main"
```

## ProjectContext lifecycle

```
MCP request arrives
       │
       ▼
get_project_context(project_root)  ← module-level registry (one per process)
       │
       ▼
ProjectContext.ensure_bootstrapped()  ← idempotent, runs once
       │  creates .memory-engine/, initializes DB, indexes seed knowledge
       │
       ▼
Tool / Resource handler
       │  calls existing service-layer components
       │  never duplicates business logic
       ▼
Structured response
```

## MCP tools

| Tool | Delegates to |
|---|---|
| `retrieve_agent_context` | `UnifiedContextRetrievalService.retrieve()` |
| `inspect_memory` | `InspectService.inspect()` |
| `inspect_knowledge` | `KnowledgeChunkORM` / file read with `resolve_safe()` |
| `reflect_and_write` | `PostTaskService.reflect_and_write()` |
| `memory_status` | `ProjectStateManager` + DB counts |
| `refresh_project_knowledge` | `ProjectBootstrapService.incremental_refresh()` |

## MCP resources

| Resource URI | Content |
|---|---|
| `memory://project/current/constraints` | Active constraint `MemoryNode` summaries |
| `memory://project/current/architecture` | Architecture / module summaries |
| `memory://project/current/status` | Bootstrap state, retrieval mode, counts |
| `memory://project/current/recent-incidents` | Recent debug/incident memories |
| `memory://project/current/memory-tree-summary` | Full tree outline |
| `memory://project/current/agent-policy` | `AGENT_MEMORY_POLICY.md` content |

## Security at the MCP boundary

1. All paths validated via `resolve_safe(path, project_root)` before reads.
2. `PathEscapeError` returned as structured error (not exception to client).
3. All content redacted via `redact()` before MCP output.
4. No raw secrets, credentials, or env file content ever returned.
5. `inspect_knowledge` path is restricted to `project_root`.

## Degraded mode in MCP responses

Every tool response includes a `meta` block:

```json
{
  "meta": {
    "retrieval_mode": "lexical_structured_fallback",
    "vector_backend": "ephemeral",
    "bootstrap_status": "READY",
    "warnings": [
      "Semantic vector retrieval is unavailable. Results use lexical, module-path, symbol, and memory-tree ranking."
    ]
  }
}
```

`bootstrap_status` values: `UNINITIALIZED | BOOTSTRAPPING | READY | INDEXING_INCREMENTAL | DEGRADED | FAILED`
