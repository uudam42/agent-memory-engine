"""Client-specific policy adapter installer (Phase 11).

Supports:
  - Claude Code: installs a generated rule file at CLAUDE.md (project root)
                 using stable begin/end markers, preserving user content.
  - Cursor:      installs a generated rule at .cursor/rules/agent-memory-policy.mdc
                 using stable begin/end markers.
  - Generic:     prints canonical policy path and integration guidance.

Safety rules:
  - Never overwrite user-authored content outside generated markers.
  - Never create files without explicit opt-in (install command).
  - Generated blocks are always idempotent and clearly marked.
"""

from __future__ import annotations

from pathlib import Path

from memory_engine.policy.generator import (
    _BEGIN_MARKER,
    _END_MARKER,
    _build_policy_block,
    generate_policy,
    policy_status,
)

_CLAUDE_MD_BEGIN = "<!-- BEGIN:MEMORY_ENGINE_POLICY (do not edit this block) -->"
_CLAUDE_MD_END = "<!-- END:MEMORY_ENGINE_POLICY -->"


def _claude_md_block(policy_path: Path, mcp_server_name: str = "memory-engine") -> str:
    return f"""{_CLAUDE_MD_BEGIN}
## Memory Engine — Agent Workflow Policy

This project uses [Agent Memory Engine](https://github.com/uudam42/agent-memory-engine)
for persistent coding memory. Follow these rules exactly.

### `{mcp_server_name}:seed_project_context` — ONCE on new project setup

Call **once** when first connecting a project to Memory Engine (empty memory database).
Provide: `description`, `constraints`, `decisions`, `tech_stack`, `conventions`.
All fields optional — README.md is auto-scanned if description is omitted.
Do NOT call on every task. Check `memory_status` active_memories > 0 to skip.

### `{mcp_server_name}:retrieve_agent_context` — BEFORE non-trivial work

**Call when the task involves any of:**
- Editing production code, tests, CI, build scripts, dependencies, or config
- Debugging a failure or investigating unexpected behaviour
- Touching a subsystem not visited yet in this session
- Changing ≥ 2 files, or any design / architecture decision
- Security, auth, schema, retry, state-machine, or persistence logic

**Skip when ALL of these are true:**
- Pure explanation with no file edits planned
- Single-file, single-line typo / whitespace fix with no logic change
- Already called for the same task in this session

Pass `task_intent` for better results: `bug_fix`, `feature_implementation`,
`architecture_review`, `refactor`. Include `current_files` and `current_symbols`.

### `{mcp_server_name}:reflect_and_write` — AFTER verified non-trivial work

**Call when ALL of these are true:**
- `verification_status` is `tests_passed` or `build_success`
- ≥ 2 files changed, OR a non-trivial architectural decision was made
- Task is complete — not exploratory, not partially done

**Skip when ANY of these is true:**
- Tests failed or the task was reverted
- Only a single trivial file changed (typo, comment wording)
- Work is exploratory / no committed changes
- No validation was run

Pass `task_intent` (same value used in retrieve) and `changed_files` list.
Do NOT inflate `verification_status` — report what actually ran.

Full policy: `{policy_path}`
{_CLAUDE_MD_END}"""


def install_claude_code(
    project_root: Path,
    mcp_server_name: str = "memory-engine",
) -> Path:
    """Install or update memory engine workflow rule in CLAUDE.md.

    The project root CLAUDE.md is the verified supported location for
    Claude Code project instructions (checked 2025-06).
    Preserves all user-authored content outside generated markers.
    """
    policy_path = project_root / ".memory-engine" / "generated" / "AGENT_MEMORY_POLICY.md"
    generate_policy(project_root, mcp_server_name)

    claude_md = project_root / "CLAUDE.md"
    new_block = _claude_md_block(policy_path, mcp_server_name)

    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if _CLAUDE_MD_BEGIN in existing and _CLAUDE_MD_END in existing:
            before = existing[: existing.index(_CLAUDE_MD_BEGIN)]
            after = existing[existing.index(_CLAUDE_MD_END) + len(_CLAUDE_MD_END):]
            content = before + new_block + after
        else:
            content = existing.rstrip() + "\n\n" + new_block + "\n"
    else:
        content = new_block + "\n"

    claude_md.write_text(content, encoding="utf-8")
    return claude_md


def install_cursor(
    project_root: Path,
    mcp_server_name: str = "memory-engine",
) -> Path:
    """Install or update a Cursor rule file for the memory engine policy.

    Creates .cursor/rules/agent-memory-policy.mdc (Cursor's verified
    project rules location as of 2025-06). Preserves existing rules.
    """
    policy_path = project_root / ".memory-engine" / "generated" / "AGENT_MEMORY_POLICY.md"
    generate_policy(project_root, mcp_server_name)

    rules_dir = project_root / ".cursor" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    rule_file = rules_dir / "agent-memory-policy.mdc"

    block = _build_policy_block(project_root, mcp_server_name)

    if rule_file.exists():
        existing = rule_file.read_text(encoding="utf-8")
        if _BEGIN_MARKER in existing and _END_MARKER in existing:
            before = existing[: existing.index(_BEGIN_MARKER)]
            after = existing[existing.index(_END_MARKER) + len(_END_MARKER):]
            content = before + block + after
        else:
            content = existing.rstrip() + "\n\n" + block + "\n"
    else:
        content = block + "\n"

    rule_file.write_text(content, encoding="utf-8")
    return rule_file


def remove_adapter(project_root: Path, client: str) -> str | None:
    """Remove a generated policy block from a client adapter file.

    Returns the path of the modified file, or None if nothing was changed.
    """
    if client == "claude-code":
        target = project_root / "CLAUDE.md"
        begin, end = _CLAUDE_MD_BEGIN, _CLAUDE_MD_END
    elif client == "cursor":
        target = project_root / ".cursor" / "rules" / "agent-memory-policy.mdc"
        begin, end = _BEGIN_MARKER, _END_MARKER
    else:
        return None

    if not target.exists():
        return None

    existing = target.read_text(encoding="utf-8")
    if begin not in existing:
        return None

    before = existing[: existing.index(begin)]
    after = existing[existing.index(end) + len(end):]
    target.write_text((before + after).strip() + "\n", encoding="utf-8")
    return str(target)


def adapter_status(project_root: Path) -> dict:
    """Return installation status for all known client adapters."""
    claude_md = project_root / "CLAUDE.md"
    cursor_rule = project_root / ".cursor" / "rules" / "agent-memory-policy.mdc"

    claude_installed = (
        claude_md.exists()
        and _CLAUDE_MD_BEGIN in claude_md.read_text(encoding="utf-8")
    )
    cursor_installed = (
        cursor_rule.exists()
        and _BEGIN_MARKER in cursor_rule.read_text(encoding="utf-8")
    )

    return {
        "policy": policy_status(project_root),
        "claude_code": {
            "path": str(claude_md),
            "installed": claude_installed,
        },
        "cursor": {
            "path": str(cursor_rule),
            "installed": cursor_installed,
        },
    }
