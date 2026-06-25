"""MCP resource implementations.

Resources (read-only):
  memory://project/current/constraints
  memory://project/current/architecture
  memory://project/current/status
  memory://project/current/recent-incidents
  memory://project/current/memory-tree-summary
  memory://project/current/agent-policy

Phase 9 additions:
  memory://project/current/git-context
  memory://project/current/branch-memory-summary
  memory://project/current/sync-status
"""

from __future__ import annotations

from typing import Any

from memory_engine.mcp.project_context import ProjectContext
from memory_engine.models.orm import MemoryNodeORM


def resource_constraints(ctx: ProjectContext) -> str:
    """Return active constraint memories as compact text."""
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    try:
        nodes = (
            session.query(MemoryNodeORM)
            .filter_by(
                project_id=ctx.get_project_id(),
                status="active",
                kind="constraint",
            )
            .order_by(MemoryNodeORM.importance.desc())
            .limit(20)
            .all()
        )
        if not nodes:
            return "# Constraints\n\nNo constraints stored yet."
        lines = ["# Project Constraints\n"]
        for n in nodes:
            lines.append(f"## {n.title}")
            lines.append(n.summary)
            lines.append("")
        return "\n".join(lines)
    finally:
        session.close()


def resource_architecture(ctx: ProjectContext) -> str:
    """Return architecture memory summaries."""
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    try:
        nodes = (
            session.query(MemoryNodeORM)
            .filter_by(
                project_id=ctx.get_project_id(),
                status="active",
            )
            .filter(MemoryNodeORM.kind.in_(["architecture", "module", "subsystem"]))
            .order_by(MemoryNodeORM.importance.desc())
            .limit(15)
            .all()
        )
        if not nodes:
            return "# Architecture\n\nNo architecture summaries stored yet."
        lines = ["# Architecture Summaries\n"]
        for n in nodes:
            lines.append(f"## {n.title}  [{n.kind}]")
            lines.append(n.summary)
            lines.append("")
        return "\n".join(lines)
    finally:
        session.close()


def resource_status(ctx: ProjectContext) -> str:
    """Return project status as compact YAML-like text."""
    from memory_engine.mcp.tools import tool_memory_status
    status = tool_memory_status(ctx)
    lines = ["# Memory Engine Status\n"]
    for k, v in status.items():
        if k != "warnings":
            lines.append(f"{k}: {v}")
    if status.get("warnings"):
        lines.append("\n## Warnings")
        for w in status["warnings"]:
            lines.append(f"- {w}")
    return "\n".join(lines)


def resource_recent_incidents(ctx: ProjectContext) -> str:
    """Return recent incident / debug memories."""
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    try:
        nodes = (
            session.query(MemoryNodeORM)
            .filter_by(
                project_id=ctx.get_project_id(),
                status="active",
            )
            .filter(MemoryNodeORM.kind.in_(["debug", "incident"]))
            .order_by(MemoryNodeORM.updated_at.desc())
            .limit(10)
            .all()
        )
        if not nodes:
            return "# Recent Incidents\n\nNo incidents stored yet."
        lines = ["# Recent Incidents\n"]
        for n in nodes:
            lines.append(f"## {n.title}")
            lines.append(f"*Confidence: {n.confidence:.2f}  Importance: {n.importance:.2f}*")
            lines.append(n.summary)
            lines.append("")
        return "\n".join(lines)
    finally:
        session.close()


def resource_memory_tree_summary(ctx: ProjectContext) -> str:
    """Return the memory tree as a compact outline."""
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    try:
        nodes = (
            session.query(MemoryNodeORM)
            .filter_by(
                project_id=ctx.get_project_id(),
                status="active",
            )
            .order_by(MemoryNodeORM.depth.asc(), MemoryNodeORM.importance.desc())
            .limit(50)
            .all()
        )
        if not nodes:
            return "# Memory Tree\n\nNo memories stored yet."
        lines = ["# Memory Tree Summary\n"]
        for n in nodes:
            indent = "  " * n.depth
            lines.append(f"{indent}- [{n.kind}] {n.title} (importance={n.importance:.2f})")
        return "\n".join(lines)
    finally:
        session.close()


def resource_agent_policy(ctx: ProjectContext) -> str:
    """Return the generated AGENT_MEMORY_POLICY.md content."""
    policy_path = ctx.storage.agent_policy_path
    if policy_path.exists():
        return policy_path.read_text(encoding="utf-8")
    # Generate on demand if missing
    ctx.ensure_bootstrapped()
    if policy_path.exists():
        return policy_path.read_text(encoding="utf-8")
    return "# Agent Memory Policy\n\nRun bootstrap to generate this file."


# ---------------------------------------------------------------------------
# Phase 9: Git-aware resources
# ---------------------------------------------------------------------------


def resource_git_context(ctx: ProjectContext) -> str:
    """Return current Git context as compact text.

    Safe: no remote URLs, no user identity, no credentials.
    """
    git_ctx = ctx.get_git_context(refresh=True)
    if not git_ctx.git_available:
        return "# Git Context\n\nGit is not available in this environment."
    if not git_ctx.is_repository:
        return "# Git Context\n\nProject root is not a Git repository."

    lines = ["# Git Context\n"]
    lines.append(f"branch: {git_ctx.current_branch or '(detached HEAD)'}")
    lines.append(f"head_commit: {git_ctx.head_commit or 'unknown'}")
    if git_ctx.base_branch:
        lines.append(f"base_branch: {git_ctx.base_branch}")
        lines.append(f"base_commit: {git_ctx.base_commit or 'unknown'}")
    lines.append(f"working_tree_dirty: {git_ctx.working_tree_dirty}")
    if git_ctx.staged_files:
        lines.append(f"\nStaged ({len(git_ctx.staged_files)}):")
        for f in git_ctx.staged_files[:20]:
            lines.append(f"  + {f}")
    if git_ctx.modified_files:
        lines.append(f"\nModified ({len(git_ctx.modified_files)}):")
        for f in git_ctx.modified_files[:20]:
            lines.append(f"  M {f}")
    if git_ctx.deleted_files:
        lines.append(f"\nDeleted ({len(git_ctx.deleted_files)}):")
        for f in git_ctx.deleted_files[:20]:
            lines.append(f"  D {f}")
    if git_ctx.renamed_files:
        lines.append(f"\nRenamed ({len(git_ctx.renamed_files)}):")
        for r in git_ctx.renamed_files[:10]:
            lines.append(f"  R {r.old_path} → {r.new_path}")
    if git_ctx.warnings:
        lines.append("\n## Warnings")
        for w in git_ctx.warnings:
            lines.append(f"- {w}")
    return "\n".join(lines)


def resource_branch_memory_summary(ctx: ProjectContext) -> str:
    """Return memories scoped to the current branch."""
    ctx.ensure_bootstrapped()
    git_ctx = ctx.get_git_context()
    current_branch = git_ctx.current_branch

    session = ctx.get_session()
    try:
        query = session.query(MemoryNodeORM).filter_by(
            project_id=ctx.get_project_id(),
            status="active",
        )
        if current_branch:
            # Prefer branch-specific, then global/mainline
            branch_nodes = query.filter(
                MemoryNodeORM.branch_name == current_branch
            ).order_by(MemoryNodeORM.importance.desc()).limit(20).all()

            global_nodes = query.filter(
                (MemoryNodeORM.branch_name == None) |  # noqa: E711
                (MemoryNodeORM.branch_scope == "global") |
                (MemoryNodeORM.branch_scope == "mainline")
            ).order_by(MemoryNodeORM.importance.desc()).limit(20).all()
        else:
            branch_nodes = []
            global_nodes = query.order_by(MemoryNodeORM.importance.desc()).limit(30).all()

        lines = [f"# Branch Memory Summary\n"]
        if current_branch:
            lines[0] = f"# Branch Memory Summary: {current_branch}\n"

        if branch_nodes:
            lines.append(f"## Branch-Specific Memories ({current_branch})\n")
            for n in branch_nodes:
                lines.append(f"### [{n.kind}] {n.title}")
                lines.append(n.summary)
                lines.append("")

        if global_nodes:
            lines.append("## Global / Mainline Memories\n")
            for n in global_nodes:
                scope = n.branch_scope or "global"
                lines.append(f"### [{n.kind}] {n.title}  [{scope}]")
                lines.append(n.summary)
                lines.append("")

        if not branch_nodes and not global_nodes:
            lines.append("No memories stored yet.")

        return "\n".join(lines)
    finally:
        session.close()


def resource_sync_status(ctx: ProjectContext) -> str:
    """Return incremental sync and Git synchronization status."""
    state_mgr = ctx.get_state_manager()
    state = state_mgr.load()
    git_ctx = ctx.get_git_context()

    lines = ["# Synchronization Status\n"]
    lines.append(f"bootstrap_status: {state.bootstrap_status}")
    lines.append(f"synchronization_status: {state.synchronization_status}")
    lines.append(f"index_revision: {state.index_revision}")
    lines.append(f"last_indexed_at: {state.last_indexed_at or 'never'}")
    lines.append(f"last_git_sync_at: {state.last_git_sync_at or 'never'}")
    lines.append(f"last_indexed_commit: {state.last_indexed_commit or 'unknown'}")
    lines.append("")
    lines.append("## Git State")
    lines.append(f"git_available: {git_ctx.git_available}")
    lines.append(f"is_repository: {git_ctx.is_repository}")
    if git_ctx.is_repository:
        lines.append(f"current_branch: {git_ctx.current_branch or '(detached)'}")
        lines.append(f"head_commit: {git_ctx.head_commit or 'unknown'}")
        lines.append(f"working_tree_dirty: {git_ctx.working_tree_dirty}")
        stale = state.last_indexed_commit and git_ctx.head_commit and (
            state.last_indexed_commit != git_ctx.head_commit
        )
        lines.append(f"index_behind_head: {bool(stale)}")
    return "\n".join(lines)
