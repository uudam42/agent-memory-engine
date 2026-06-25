"""MCP resource implementations.

Resources (read-only):
  memory://project/current/constraints
  memory://project/current/architecture
  memory://project/current/status
  memory://project/current/recent-incidents
  memory://project/current/memory-tree-summary
  memory://project/current/agent-policy
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
