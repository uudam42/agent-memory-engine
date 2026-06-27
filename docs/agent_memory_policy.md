# Agent Memory Policy (Phase 11)

## Overview

Phase 11 introduces a canonical **AGENT_MEMORY_POLICY.md** that instructs compliant
MCP coding agents (Claude Code, Cursor, etc.) on the mandatory memory workflow for
each project.

The policy is generated deterministically into `.memory-engine/generated/AGENT_MEMORY_POLICY.md`
and can be installed into client-specific adapter locations.

---

## Policy Workflow Diagram

```
Non-trivial task received
      │
      ▼
retrieve_agent_context required
      │
      ▼
Inspect memory, branch, revision, and source evidence
      │
      ▼
Implement changes
      │
      ▼
Run validation
      │
      ├── validation passes
      │       ▼
      │   reflect_and_write required
      │       ▼
      │   candidate gating / promotion / retention
      │
      └── validation fails or task reverts
              ▼
        do not claim verified durable knowledge
```

---

## Generated File Location

```
<project_root>/.memory-engine/generated/AGENT_MEMORY_POLICY.md
```

This file uses stable begin/end markers:

```
<!-- BEGIN:AGENT_MEMORY_POLICY (do not edit this block) -->
...generated content...
<!-- END:AGENT_MEMORY_POLICY -->
```

Regenerating the policy replaces only the content inside the markers. Any
user-authored content outside the markers is preserved.

---

## CLI Commands

```bash
# Generate canonical policy for a project
memory policy generate --project-root .

# Install client-specific adapters
memory policy install --project-root . --client claude-code
memory policy install --project-root . --client cursor

# Check installation status
memory policy status --project-root .

# Remove a generated policy block
memory policy remove --project-root . --client claude-code
memory policy remove --project-root . --client cursor
```

---

## Client Adapters

### Claude Code

Installs a generated block into `CLAUDE.md` at the project root.
This is the verified location for Claude Code project instructions (checked 2025-06).

Adapter markers:
```
<!-- BEGIN:MEMORY_ENGINE_POLICY (do not edit this block) -->
...
<!-- END:MEMORY_ENGINE_POLICY -->
```

Preserves all user-authored content outside the markers.

### Cursor

Installs a generated rule file into:
```
.cursor/rules/agent-memory-policy.mdc
```

This is the verified Cursor project rules location (checked 2025-06).
Existing `.cursor/rules/*.mdc` files are not affected.

### Generic MCP Clients

Generate only the canonical policy file and provide the path:
```
.memory-engine/generated/AGENT_MEMORY_POLICY.md
```

Point your client's project instructions to this file.

---

## Policy Content

The generated policy includes these sections:

| Section | Content |
|---|---|
| Purpose | Why the workflow matters |
| Task Classification | Non-trivial vs. trivial definition |
| Pre-Task Recall Rule | MUST call `retrieve_agent_context` |
| Post-Task Reflection Rule | MUST call `reflect_and_write` after validation |
| Source-of-Truth & Safety | Memory is advisory; safety boundaries |
| Branch-Aware Rules | Current branch preference; promotion requirements |
| Retrieval Granularity | When to use propositions vs. paragraphs vs. summaries |
| Compliance Checklist | Before/after checklist |
| Transparency Notice | Compliance limitation statement |

---

## Compliance Limitations

This policy is a **project instruction** for MCP-aware coding agents.

**The MCP server cannot technically force every arbitrary client or model to invoke
tools.** Compliance depends on the client honouring project-level instruction files.
Clients that do not read project instructions will not automatically comply.

This is stated explicitly in the generated policy under "Transparency Notice".

---

## Idempotency

Running `memory policy generate` multiple times:

- Creates the file if it does not exist
- Replaces only the generated block (between markers)
- Preserves user-authored content outside markers
- Updates the `policy_version` and `generated` timestamp
- Never creates duplicate generated blocks

Running `memory policy install --client <name>` multiple times:

- Updates the adapter block in place
- Preserves all other content in the target file
- Never creates duplicate adapter blocks

---

## Bootstrap Integration

On first bootstrap, `memory policy generate` is available as an explicit command.
Default bootstrap does **not** silently modify CLAUDE.md or `.cursor/rules/`.
Auto-configuration requires explicit CLI opt-in.

---

## Windows PowerShell Installer Opt-in

The Windows installer (`scripts/install.ps1`) supports opt-in policy installation:

```powershell
.\scripts\install.ps1 -ConfigureClaudeCode
.\scripts\install.ps1 -ConfigureCursor
```

These flags trigger `memory policy install` for the respective client after
dependency installation completes.
