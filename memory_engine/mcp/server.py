"""memory-engine-mcp — Python-native MCP server entry point.

Transport: stdio (the MCP client launches this process directly).

Usage:
  memory-engine-mcp --project-root /path/to/target-project

or via uv:
  uv run --directory /path/to/memory-engine memory-engine-mcp \\
    --project-root /path/to/target-project

or via environment variable:
  MEMORY_ENGINE_PROJECT_ROOT=/path/to/project uv run memory-engine-mcp

Architecture:
  MCP Client (Cursor / Claude Code / etc.)
    → stdio
    → This process (memory-engine-mcp)
    → memory_engine.mcp.tools / resources
    → Existing Memory Engine service layer
    → Project-local .memory-engine/ storage
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from memory_engine.bootstrap.project_root import ProjectRootError, resolve_project_root
from memory_engine.mcp.errors import MCPProjectError
from memory_engine.mcp.project_context import ProjectContext, get_project_context
from memory_engine.mcp.schemas import (
    InspectKnowledgeInput,
    InspectMemoryInput,
    ReflectAndWriteInput,
    RetrieveContextInput,
)
from memory_engine.mcp.tools import (
    tool_inspect_knowledge,
    tool_inspect_memory,
    tool_memory_status,
    tool_reflect_and_write,
    tool_refresh_project_knowledge,
    tool_retrieve_agent_context,
)
from memory_engine.mcp.resources import (
    resource_agent_policy,
    resource_architecture,
    resource_constraints,
    resource_memory_tree_summary,
    resource_recent_incidents,
    resource_status,
    resource_git_context,
    resource_branch_memory_summary,
    resource_sync_status,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="memory-engine-mcp",
        description="Memory Engine MCP server — persistent memory for coding agents.",
    )
    p.add_argument("--project-root", default=None, help="Absolute path to target project")
    p.add_argument("--config-path", default=None, help="Override config.yaml path")
    p.add_argument("--log-level", default="WARNING", help="Logging level (default: WARNING)")
    p.add_argument("--disable-auto-index", action="store_true", help="Skip indexing on start")
    p.add_argument("--privacy-mode", action="store_true", help="Extra-strict privacy mode")
    p.add_argument("--version", action="version", version="memory-engine 0.1.0 (Phase 10)")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# FastMCP server factory
# ---------------------------------------------------------------------------


def create_mcp_server(project_root: Path) -> FastMCP:  # type: ignore[return]
    """Build and return a configured FastMCP server bound to project_root."""

    ctx = get_project_context(project_root)
    mcp_server = FastMCP(
        name="memory-engine",
        instructions=(
            "Memory Engine gives coding agents persistent project memory and grounded "
            "project knowledge. Call retrieve_agent_context before non-trivial coding "
            "work, and reflect_and_write after successful validated work."
        ),
    )

    # ── Tools ────────────────────────────────────────────────────────────────

    @mcp_server.tool(
        name="retrieve_agent_context",
        description=(
            "Retrieve the smallest relevant set of persistent memory and grounded "
            "project knowledge before non-trivial coding work. "
            "Bootstraps the project automatically on first use. "
            "Pass current_branch and head_commit to enable branch-aware retrieval "
            "(Phase 9): memories from the current branch are ranked higher than "
            "mainline or unrelated branches. "
            "Phase 10 — multi-granularity routing: pass task_intent (e.g. 'bug_fix', "
            "'architecture_review', 'feature_implementation') to route retrieval to "
            "the optimal knowledge granularity layer (proposition / paragraph / "
            "module summary). Pass preferred_layers to override the router, and "
            "proposition_types to filter by type (e.g. 'security_rule', 'constraint', "
            "'risk', 'architecture', 'decision'). Results appear in multigranular_chunks."
        ),
    )
    async def retrieve_agent_context(
        task: str,
        current_files: list[str] | None = None,
        current_symbols: list[str] | None = None,
        token_budget: int = 6000,
        current_branch: str | None = None,
        head_commit: str | None = None,
        # Phase 10: multi-granularity routing
        task_intent: str = "unknown",
        preferred_layers: list[str] | None = None,
        proposition_types: list[str] | None = None,
    ) -> dict:  # type: ignore[type-arg]
        inp = RetrieveContextInput(
            task=task,
            current_files=current_files or [],
            current_symbols=current_symbols or [],
            token_budget=token_budget,
            current_branch=current_branch,
            head_commit=head_commit,
            task_intent=task_intent,
            preferred_layers=preferred_layers or [],
            proposition_types=proposition_types,
        )
        return tool_retrieve_agent_context(ctx, inp)

    @mcp_server.tool(
        name="inspect_memory",
        description=(
            "Progressively inspect a MemoryNode, its children, relations, and "
            "relevant evidence. Use after retrieve_agent_context when more depth "
            "is needed on a specific memory."
        ),
    )
    async def inspect_memory(
        memory_id: str,
        inspection_depth: int = 1,
        include_evidence: bool = False,
        current_task: str | None = None,
    ) -> dict:  # type: ignore[type-arg]
        inp = InspectMemoryInput(
            memory_id=memory_id,
            inspection_depth=inspection_depth,
            include_evidence=include_evidence,
            current_task=current_task,
        )
        return tool_inspect_memory(ctx, inp)

    @mcp_server.tool(
        name="inspect_knowledge",
        description=(
            "Inspect a KnowledgeChunk or source-grounded file range within the "
            "target project. Paths are restricted to the project root. "
            "Content is redacted before output."
        ),
    )
    async def inspect_knowledge(
        chunk_id: str | None = None,
        path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        include_content: bool = True,
    ) -> dict:  # type: ignore[type-arg]
        inp = InspectKnowledgeInput(
            chunk_id=chunk_id,
            path=path,
            start_line=start_line,
            end_line=end_line,
            include_content=include_content,
        )
        return tool_inspect_knowledge(ctx, inp)

    @mcp_server.tool(
        name="reflect_and_write",
        description=(
            "Report a completed validated task to the post-task reflection pipeline. "
            "The system decides whether and how to retain knowledge — agents cannot "
            "force memory creation directly. "
            "Do not call for trivial, failed, reverted, or unverified work. "
            "verification_status must be one of: tests_passed, build_success, "
            "manual_check, tests_failed, unverified. "
            "Pass current_branch and head_commit (Phase 9) to scope the written "
            "memory to the correct branch; omit to auto-detect from Git."
        ),
    )
    async def reflect_and_write(
        task: str,
        outcome: str,
        verification_status: str = "unverified",
        changed_files: list[str] | None = None,
        task_summary: str | None = None,
        test_summary: str | None = None,
        current_branch: str | None = None,
        head_commit: str | None = None,
    ) -> dict:  # type: ignore[type-arg]
        inp = ReflectAndWriteInput(
            task=task,
            outcome=outcome,
            verification_status=verification_status,
            changed_files=changed_files or [],
            task_summary=task_summary,
            test_summary=test_summary,
            current_branch=current_branch,
            head_commit=head_commit,
        )
        return tool_reflect_and_write(ctx, inp)

    @mcp_server.tool(
        name="memory_status",
        description=(
            "Return project health, bootstrap state, retrieval mode, memory counts, "
            "knowledge index status, and cache state."
        ),
    )
    async def memory_status() -> dict:  # type: ignore[type-arg]
        return tool_memory_status(ctx)

    @mcp_server.tool(
        name="refresh_project_knowledge",
        description=(
            "Explicit-use only. Trigger a safe incremental rescan of changed project "
            "sources. Not needed for normal workflow — indexing runs automatically "
            "on bootstrap. Returns a summary of changed, added, and removed sources."
        ),
    )
    async def refresh_project_knowledge() -> dict:  # type: ignore[type-arg]
        return tool_refresh_project_knowledge(ctx)

    # ── Resources ────────────────────────────────────────────────────────────

    @mcp_server.resource("memory://project/current/constraints")
    async def get_constraints() -> str:
        return resource_constraints(ctx)

    @mcp_server.resource("memory://project/current/architecture")
    async def get_architecture() -> str:
        return resource_architecture(ctx)

    @mcp_server.resource("memory://project/current/status")
    async def get_status() -> str:
        return resource_status(ctx)

    @mcp_server.resource("memory://project/current/recent-incidents")
    async def get_recent_incidents() -> str:
        return resource_recent_incidents(ctx)

    @mcp_server.resource("memory://project/current/memory-tree-summary")
    async def get_memory_tree_summary() -> str:
        return resource_memory_tree_summary(ctx)

    @mcp_server.resource("memory://project/current/agent-policy")
    async def get_agent_policy() -> str:
        return resource_agent_policy(ctx)

    # Phase 9: Git-aware resources
    @mcp_server.resource("memory://project/current/git-context")
    async def get_git_context() -> str:
        return resource_git_context(ctx)

    @mcp_server.resource("memory://project/current/branch-memory-summary")
    async def get_branch_memory_summary() -> str:
        return resource_branch_memory_summary(ctx)

    @mcp_server.resource("memory://project/current/sync-status")
    async def get_sync_status() -> str:
        return resource_sync_status(ctx)

    return mcp_server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    try:
        project_root = resolve_project_root(explicit=args.project_root)
    except ProjectRootError as exc:
        print(f"[memory-engine-mcp] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    logger.info("Memory Engine MCP server starting (project: %s)", project_root)

    mcp_server = create_mcp_server(project_root)

    # Run via stdio (FastMCP default)
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
