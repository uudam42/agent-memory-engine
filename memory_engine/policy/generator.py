"""AGENT_MEMORY_POLICY.md generator (Phase 11).

Generates a canonical policy file at:
  <project_root>/.memory-engine/generated/AGENT_MEMORY_POLICY.md

Uses stable begin/end markers so regeneration is idempotent and any
user-authored content outside the markers is preserved.

The policy instructs compliant MCP coding agents (Claude Code, Cursor, etc.)
to retrieve memory before non-trivial work and reflect after validated work.

NOTE: This policy is advisory. The MCP server cannot technically force every
arbitrary client or model to invoke tools. Compliance depends on the client
honouring project instructions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

POLICY_VERSION = "1.0"

_BEGIN_MARKER = "<!-- BEGIN:AGENT_MEMORY_POLICY (do not edit this block) -->"
_END_MARKER = "<!-- END:AGENT_MEMORY_POLICY -->"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_policy_block(
    project_root: Path,
    mcp_server_name: str = "memory-engine",
    policy_version: str = POLICY_VERSION,
) -> str:
    ts = _now_iso()
    return f"""{_BEGIN_MARKER}
# Agent Memory Policy
<!-- policy_version: {policy_version} | generated: {ts} | server: {mcp_server_name} -->

## Purpose

This policy preserves reliable project context while preventing memory pollution.
All compliant MCP coding agents operating in this project MUST follow this workflow.

The MCP server cannot technically force every arbitrary client or model to invoke
tools. This policy provides strong workflow guidance for clients that honour
project-level instructions.

---

## Task Classification

Treat a task as **non-trivial** if it involves any of:

- Editing production code, tests, build scripts, CI, dependencies, schemas, or configuration
- Changing public APIs or interfaces
- Debugging a defect or investigating a failure
- Changing security, authorization, validation, retry, state-machine, or persistence behaviour
- Modifying multiple files
- Making a design or architecture decision
- Refactoring behaviorally meaningful code
- Working on branch-specific implementation

Treat a task as **trivial** only when clearly limited to:

- Spelling correction
- Comment-only wording change
- Formatting-only change
- Pure explanation with no repository modification
- A user-requested one-line non-behavioral edit

**When uncertain, classify the task as non-trivial.**

---

## Mandatory Pre-Task Recall Rule

Before editing files for any non-trivial task, the agent **MUST** call
`{mcp_server_name}:retrieve_agent_context`.

The agent MUST provide:
- a concise task description
- affected files, modules, or symbols when known
- `task_intent` when supported (e.g. `bug_fix`, `architecture_review`, `feature_implementation`)
- current branch context when relevant

The agent MUST inspect returned constraints, incidents, decisions, source evidence,
branch scope, revision state, and retrieval trace before making behaviorally
meaningful changes.

---

## Mandatory Post-Task Reflection Rule

After a non-trivial task has been implemented and validation has passed, the agent
**MUST** call `{mcp_server_name}:reflect_and_write`.

The reflection MUST include:
- task summary and implementation changes
- files and modules changed
- validation performed (test commands and outcomes)
- whether the task succeeded, failed, was reverted, or remains unverified
- branch and revision context when available
- durable lessons, constraints, incidents, procedures, or decisions worth retaining

**Rules:**
- Do not claim successful validation when no validation ran.
- Do not write speculative or unverified facts as durable memory.
- Do not call failed or reverted work verified.
- If validation fails, only submit reflection if a useful failure record exists.

---

## Source-of-Truth and Safety Rules

Memory is advisory context — not stronger authority than current source code,
tests, configuration, or user requirements.

When memory conflicts with verified current source evidence:
- Prefer verified current evidence.
- Report the conflict.
- Allow the lifecycle system to mark memory stale, supersede it, or request review.

Never expose secrets, credentials, tokens, private keys, remote URLs, or user
identity information through memory content.

Never bypass path sandboxing, symlink protection, or Git safety restrictions.

---

## Branch-Aware Rules

- Current-branch memory should be preferred for current-branch work.
- Feature-branch memory must not be treated as mainline truth unless explicitly promoted.
- Historical, stale, archived, and superseded memory must not silently override
  current revision evidence.

---

## Retrieval Granularity Rules

| Situation | Preferred retrieval |
|---|---|
| Exact constraints or security rules | `proposition`-level, `proposition_types=["security_rule","constraint","risk"]` |
| Implementation questions | Paragraph or symbol-local context |
| Architecture questions | Module or document summaries, then supporting evidence |
| Debugging / change-impact | Current-branch, changed-file, and incident context |

Pass `task_intent` to `retrieve_agent_context` to activate granularity routing:

```
task_intent = "bug_fix"            # → propositions first
task_intent = "architecture_review" # → module summaries first
task_intent = "feature_implementation" # → paragraphs + propositions + summaries
```

---

## Compliance Checklist

**Before non-trivial work:**
- [ ] Called `retrieve_agent_context`
- [ ] Reviewed memory and source evidence
- [ ] Checked branch/revision relevance

**After successful validated non-trivial work:**
- [ ] Ran appropriate validation
- [ ] Called `reflect_and_write`
- [ ] Reported actual verification status
- [ ] Avoided speculative memory writes

---

## Transparency Notice

This policy is a project instruction for MCP-aware coding agents.
It provides strong workflow guidance for clients that honour project rules.
The MCP server cannot technically force every arbitrary client or model to invoke tools.

Policy version: {policy_version}
Project root: {project_root}
MCP server: {mcp_server_name}
{_END_MARKER}"""


def generate_policy(
    project_root: Path,
    mcp_server_name: str = "memory-engine",
    *,
    output_path: Path | None = None,
) -> Path:
    """Generate (or regenerate) AGENT_MEMORY_POLICY.md.

    Idempotent: replaces only the content inside the stable begin/end markers.
    User-authored content outside the markers is preserved.

    Returns the path to the written file.
    """
    if output_path is None:
        out_dir = project_root / ".memory-engine" / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / "AGENT_MEMORY_POLICY.md"

    new_block = _build_policy_block(project_root, mcp_server_name)

    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
        # Replace only the generated block; preserve content outside markers
        if _BEGIN_MARKER in existing and _END_MARKER in existing:
            before = existing[: existing.index(_BEGIN_MARKER)]
            after = existing[existing.index(_END_MARKER) + len(_END_MARKER):]
            content = before + new_block + after
        else:
            # No existing markers — prepend the block
            content = new_block + "\n\n" + existing
    else:
        content = new_block + "\n"

    output_path.write_text(content, encoding="utf-8")
    return output_path


def policy_status(project_root: Path) -> dict:
    """Return status of the generated policy file."""
    policy_path = project_root / ".memory-engine" / "generated" / "AGENT_MEMORY_POLICY.md"
    if not policy_path.exists():
        return {"exists": False, "path": str(policy_path)}

    content = policy_path.read_text(encoding="utf-8")
    has_markers = _BEGIN_MARKER in content and _END_MARKER in content

    version = None
    for line in content.splitlines():
        if "policy_version:" in line:
            try:
                version = line.split("policy_version:")[1].split("|")[0].strip()
            except IndexError:
                pass
            break

    return {
        "exists": True,
        "path": str(policy_path),
        "has_markers": has_markers,
        "policy_version": version,
    }
