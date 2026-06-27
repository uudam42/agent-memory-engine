"""MCP tool implementations — thin wrappers over existing service layer.

No business logic is duplicated here.  Each tool:
  1. Resolves and bootstraps the project context.
  2. Calls the appropriate existing service.
  3. Returns a structured, agent-friendly response.

Tools:
  retrieve_agent_context
  inspect_memory
  inspect_knowledge
  reflect_and_write
  memory_status
  refresh_project_knowledge
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from memory_engine.bootstrap.security import PathEscapeError, resolve_safe
from memory_engine.bootstrap.vector_fallback import degraded_response_metadata
from memory_engine.knowledge.fusion import UnifiedContextRetrievalService
from memory_engine.knowledge.search import KnowledgeSearchService
from memory_engine.mcp.project_context import ProjectContext
from memory_engine.mcp.schemas import (
    InspectKnowledgeInput,
    InspectMemoryInput,
    MemoryStatusOutput,
    ReflectAndWriteInput,
    ReflectWriteOutput,
    RetrievalMeta,
    RetrieveContextInput,
    RetrieveContextOutput,
)
from memory_engine.models.domain import (
    ReflectionInput,
    TaskIntent,
    TaskOutcome,
    VerificationStatus,
)
from memory_engine.models.knowledge_domain import UnifiedRetrievalRequest
from memory_engine.models.knowledge_orm import KnowledgeChunkORM, KnowledgeDocumentORM
from memory_engine.models.orm import MemoryNodeORM
from memory_engine.services.post_task import PostTaskService
from memory_engine.skills.inspect import InspectService
from memory_engine.skills.recall import RecallService


# ---------------------------------------------------------------------------
# 1. retrieve_agent_context
# ---------------------------------------------------------------------------


def tool_retrieve_agent_context(
    ctx: ProjectContext,
    inp: RetrieveContextInput,
) -> dict[str, Any]:
    """Retrieve smallest relevant memory + knowledge before non-trivial work."""
    bootstrap_report = ctx.ensure_bootstrapped()
    mode_info = ctx.get_mode_info()

    # Skip if user flagged isolated task
    if inp.user_instruction_flags.get("isolated_task") or \
       inp.user_instruction_flags.get("do_not_use_memory"):
        return {
            "task": inp.task,
            "skipped": True,
            "reason": "isolated_task or do_not_use_memory flag set",
            "meta": RetrievalMeta(
                retrieval_mode=mode_info.mode,
                vector_backend=mode_info.vector_backend,
                bootstrap_status=bootstrap_report.get("bootstrap_status", "UNKNOWN"),
            ).model_dump(),
        }

    # Phase 9: resolve Git context for branch-aware retrieval
    git_ctx = ctx.get_git_context()
    effective_branch = inp.current_branch or (
        git_ctx.current_branch if git_ctx.is_repository else None
    )
    effective_commit = inp.head_commit or (
        git_ctx.head_commit if git_ctx.is_repository else None
    )
    modified_files = list(git_ctx.modified_files) + list(git_ctx.staged_files)

    session = ctx.get_session()
    try:
        svc = UnifiedContextRetrievalService(
            session,
            vector_index=ctx.get_vector_index(),
            cache=ctx.get_cache(),
        )
        pack = svc.retrieve(UnifiedRetrievalRequest(
            project_id=uuid.UUID(ctx.get_project_id()),
            task=inp.task,
            current_files=inp.current_files,
            current_symbols=inp.current_symbols,
            token_budget=inp.token_budget,
            current_branch=effective_branch,
            head_commit=effective_commit,
            modified_files=modified_files,
            # Phase 10: granularity routing hints
            task_intent=inp.task_intent,
            preferred_layers=inp.preferred_layers,
            proposition_types=inp.proposition_types,
        ))

        meta = RetrievalMeta(
            retrieval_mode=mode_info.mode,
            vector_backend=mode_info.vector_backend,
            bootstrap_status=bootstrap_report.get("bootstrap_status", "READY"),
            warnings=mode_info.warnings,
            current_branch=effective_branch,
            head_commit=effective_commit,
            branch_aware_ranking=effective_branch is not None,
            git_available=git_ctx.git_available,
            is_repository=git_ctx.is_repository,
        )

        return {
            "task": inp.task,
            "constraints": [_node_dict(n) for n in pack.constraints],
            "architecture": [_node_dict(n) for n in pack.architecture],
            "modules": [_node_dict(n) for n in pack.modules],
            "decisions": [_node_dict(n) for n in pack.decisions],
            "incidents": [_node_dict(n) for n in pack.incidents],
            "procedures": [_node_dict(n) for n in pack.procedures],
            "knowledge_chunks": [_chunk_section_dict(k) for k in pack.knowledge_chunks],
            "multigranular_chunks": [_chunk_section_dict(k) for k in pack.multigranular_chunks],
            "retrieval_trace": [t.model_dump() for t in pack.retrieval_trace[:20]],
            "total_token_estimate": pack.total_token_estimate,
            "token_budget": pack.token_budget,
            "memory_results_count": pack.memory_results_count,
            "knowledge_results_count": pack.knowledge_results_count,
            "multigranular_results_count": pack.multigranular_results_count,
            "cache_hit": pack.cache_hit,
            "meta": meta.model_dump(),
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 2. inspect_memory
# ---------------------------------------------------------------------------


def tool_inspect_memory(
    ctx: ProjectContext,
    inp: InspectMemoryInput,
) -> dict[str, Any]:
    """Progressively inspect a MemoryNode."""
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    try:
        svc = InspectService(session)
        from memory_engine.models.domain import InspectRequest
        result = svc.inspect(InspectRequest(
            project_id=uuid.UUID(ctx.get_project_id()),
            memory_id=inp.memory_id,
            inspection_depth=inp.inspection_depth,
            include_evidence=inp.include_evidence,
            current_task=inp.current_task or "",
        ))
        return result.model_dump()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 3. inspect_knowledge
# ---------------------------------------------------------------------------


def tool_inspect_knowledge(
    ctx: ProjectContext,
    inp: InspectKnowledgeInput,
) -> dict[str, Any]:
    """Inspect a KnowledgeChunk or source-grounded range."""
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    project_root = ctx.project_root

    try:
        # Path-based inspection
        if inp.path is not None:
            try:
                safe_path = resolve_safe(inp.path, project_root)
            except PathEscapeError as exc:
                return {"error": str(exc), "boundary_violation": True}

            if not safe_path.is_file():
                return {"error": f"File not found: {inp.path}"}

            content = safe_path.read_text(encoding="utf-8", errors="replace")
            if inp.start_line is not None and inp.end_line is not None:
                lines = content.splitlines()
                start = max(0, inp.start_line - 1)
                end = min(len(lines), inp.end_line)
                content = "\n".join(lines[start:end])

            from memory_engine.knowledge.redaction import redact
            redacted, _ = redact(content)
            rel_path = str(safe_path.relative_to(project_root))
            return {
                "path": rel_path,
                "start_line": inp.start_line,
                "end_line": inp.end_line,
                "content": redacted if inp.include_content else None,
                "token_estimate": max(1, len(redacted) // 4),
            }

        # Chunk-ID based inspection
        if inp.chunk_id is not None:
            chunk = session.get(KnowledgeChunkORM, inp.chunk_id)
            if chunk is None:
                return {"error": f"Chunk {inp.chunk_id!r} not found"}
            from memory_engine.knowledge.redaction import redact
            content, _ = redact(chunk.content)
            return {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "source_path": chunk.source_path,
                "source_type": chunk.source_type,
                "heading_path": chunk.heading_path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "content": content if inp.include_content else None,
                "module_paths": chunk.module_paths,
                "related_symbols": chunk.related_symbols,
                "index_status": chunk.index_status,
            }

        return {"error": "Provide chunk_id or path"}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 4. reflect_and_write
# ---------------------------------------------------------------------------


def tool_reflect_and_write(
    ctx: ProjectContext,
    inp: ReflectAndWriteInput,
) -> dict[str, Any]:
    """Report completed work to the post-task reflection pipeline."""
    bootstrap_report = ctx.ensure_bootstrapped()
    mode_info = ctx.get_mode_info()

    # Map verification_status string to enum
    try:
        ver_status = VerificationStatus(inp.verification_status)
    except ValueError:
        ver_status = VerificationStatus.unverified

    # Map outcome string to enum — use word-boundary regex to avoid false
    # positives like "ErrorActionPreference" matching "error".
    import re as _re
    task_outcome = TaskOutcome.completed  # default
    outcome_lower = inp.outcome.lower()
    def _word_match(text: str, words: tuple) -> bool:
        return any(_re.search(rf'\b{_re.escape(w)}\b', text) for w in words)
    if _word_match(outcome_lower, ("failed", "could not", "error", "broken")):
        task_outcome = TaskOutcome.failed
    elif _word_match(outcome_lower, ("revert", "rolled back")):
        task_outcome = TaskOutcome.reverted
    elif _word_match(outcome_lower, ("partial", "incomplete", "progress")):
        task_outcome = TaskOutcome.partially_completed

    # Phase 9: resolve branch info for the reflection
    git_ctx = ctx.get_git_context()
    effective_branch = inp.current_branch or (
        git_ctx.current_branch if git_ctx.is_repository else None
    )
    effective_commit = inp.head_commit or (
        git_ctx.head_commit if git_ctx.is_repository else None
    )
    branch_scope = (
        "current_branch" if (effective_branch and not _is_mainline(effective_branch))
        else "mainline" if effective_branch
        else "global"
    )

    session = ctx.get_session()
    try:
        svc = PostTaskService(session)
        # Phase 11: honour explicit task_intent from the agent
        explicit_intent: TaskIntent | None = None
        if inp.task_intent:
            try:
                explicit_intent = TaskIntent(inp.task_intent)
            except ValueError:
                explicit_intent = None

        reflection_input = ReflectionInput(
            project_id=uuid.UUID(ctx.get_project_id()),
            task_description=inp.task,
            task_outcome=task_outcome,
            outcome_summary=inp.outcome,
            verification_status=ver_status,
            touched_files=inp.changed_files,
            agent_confidence=0.85 if ver_status != VerificationStatus.unverified else 0.65,
            branch_name=effective_branch,
            head_commit=effective_commit,
            branch_scope=branch_scope,
            task_intent=explicit_intent,
        )
        result = svc.reflect_and_write(reflection_input)

        # Bump memory revision in project state
        state_mgr = ctx.get_state_manager()
        state = state_mgr.load()
        state.bump_memory()
        state_mgr.save()
        ctx.get_cache().invalidate_project(ctx.get_project_id())

        if result.reflection_skipped:
            outcome_str = "skipped"
        elif result.candidates_needs_review > 0:
            outcome_str = "needs_review"
        else:
            outcome_str = "persisted" if result.candidates_promoted > 0 else "skipped"

        meta = RetrievalMeta(
            retrieval_mode=mode_info.mode,
            vector_backend=mode_info.vector_backend,
            bootstrap_status=bootstrap_report.get("bootstrap_status", "READY"),
        )

        return ReflectWriteOutput(
            outcome=outcome_str,  # type: ignore[arg-type]
            skip_reason=result.skip_reason.value if result.skip_reason else None,
            candidates_staged=result.candidates_staged,
            candidates_promoted=result.candidates_promoted,
            candidates_discarded=result.candidates_discarded,
            consolidation_notes=result.consolidation_notes[:5],
            meta=meta,
        ).model_dump()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 5. memory_status
# ---------------------------------------------------------------------------


def tool_memory_status(ctx: ProjectContext) -> dict[str, Any]:
    """Return project health and index status."""
    bootstrap_report = ctx.ensure_bootstrapped()
    mode_info = ctx.get_mode_info()
    state_mgr = ctx.get_state_manager()
    state = state_mgr.load()

    session = ctx.get_session()
    try:
        active = session.query(MemoryNodeORM).filter_by(
            project_id=ctx.get_project_id(), status="active"
        ).count()
        stale = session.query(MemoryNodeORM).filter_by(
            project_id=ctx.get_project_id(), status="stale"
        ).count()
        docs = session.query(KnowledgeDocumentORM).filter_by(
            project_id=ctx.get_project_id(), status="indexed"
        ).count()
        chunks = session.query(KnowledgeChunkORM).filter_by(
            project_id=ctx.get_project_id(), index_status="indexed"
        ).count()
    finally:
        session.close()

    # Phase 9: include git context in status
    git_ctx = ctx.get_git_context()

    return MemoryStatusOutput(
        project_name=ctx.project_root.name,
        project_root=str(ctx.project_root),
        bootstrap_status=bootstrap_report.get("bootstrap_status", state.bootstrap_status),
        retrieval_mode=mode_info.mode,
        vector_backend=mode_info.vector_backend,
        active_memories=active,
        stale_memories=stale,
        knowledge_documents=docs,
        knowledge_chunks=chunks,
        last_indexed_at=state.last_indexed_at,
        project_revision=state.project_revision,
        memory_revision=state.memory_revision,
        knowledge_revision=state.knowledge_revision,
        index_revision=state.index_revision,
        cache_enabled=True,
        warnings=mode_info.warnings,
        # Phase 9 fields
        current_branch=git_ctx.current_branch,
        head_commit=git_ctx.head_commit,
        base_branch=git_ctx.base_branch,
        git_available=git_ctx.git_available,
        is_repository=git_ctx.is_repository,
        working_tree_dirty=git_ctx.working_tree_dirty,
        staged_files_count=len(git_ctx.staged_files),
        modified_files_count=len(git_ctx.modified_files),
        last_git_sync_at=state.last_git_sync_at,
        branch_aware_retrieval_enabled=state.branch_aware_retrieval_enabled,
        synchronization_status=state.synchronization_status,
    ).model_dump()


# ---------------------------------------------------------------------------
# 6. refresh_project_knowledge
# ---------------------------------------------------------------------------


def tool_refresh_project_knowledge(ctx: ProjectContext) -> dict[str, Any]:
    """Trigger safe incremental rescan of changed sources."""
    return ctx.incremental_refresh()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_dict(node: Any) -> dict[str, Any]:
    if hasattr(node, "model_dump"):
        return node.model_dump()
    return dict(node) if isinstance(node, dict) else str(node)  # type: ignore[arg-type]


def _chunk_section_dict(section: Any) -> dict[str, Any]:
    if hasattr(section, "model_dump"):
        return section.model_dump()
    return dict(section) if isinstance(section, dict) else {}


_MAINLINE_BRANCHES = frozenset({"main", "master", "develop", "development", "trunk"})


def _is_mainline(branch: str) -> bool:
    return branch in _MAINLINE_BRANCHES
