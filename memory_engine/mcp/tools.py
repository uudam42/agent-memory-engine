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

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from memory_engine.bootstrap.security import PathEscapeError, resolve_safe
from memory_engine.bootstrap.vector_fallback import degraded_response_metadata
from memory_engine.knowledge.fusion import UnifiedContextRetrievalService
from memory_engine.knowledge.search import KnowledgeSearchService
from memory_engine.mcp.errors import MCPWorkspaceMismatchError
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
    SeedProjectInput,
    SeedProjectOutput,
)

_LOG = logging.getLogger(__name__)
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
# Phase 14: workspace isolation helpers
# ---------------------------------------------------------------------------

_STRICT_WORKSPACE_ENV = "MEMORY_ENGINE_STRICT_WORKSPACE"


def _bump_memory_revision(ctx: ProjectContext) -> None:
    """Persist an increment to ProjectState.memory_revision.

    Phase 15 follow-up (Task 5): passed into UnifiedContextRetrievalService as
    a revision_hook so that a source-validity transition discovered *during*
    retrieval (not just during reflect_and_write) also invalidates the
    on-disk generation counter that participates in the retrieval cache key
    (memory_generation — see knowledge/cache.py). Mirrors the existing bump
    performed after reflect_and_write below.
    """
    state_mgr = ctx.get_state_manager()
    state = state_mgr.load()
    state.bump_memory()
    state_mgr.save()


def _validate_workspace(
    ctx: ProjectContext,
    workspace_root: str | None,
    repository_fingerprint: str | None,
) -> dict[str, Any] | None:
    """Validate caller-provided workspace context against the server's project.

    Returns None when validation passes.
    Returns a structured error dict when validation fails (caller must surface it).

    In compatibility mode (default): missing workspace context produces a warning
    but does not block the request.
    In strict mode (MEMORY_ENGINE_STRICT_WORKSPACE=1): missing workspace context
    blocks the request with PROJECT_CONTEXT_UNVERIFIABLE.

    Rules:
    - workspace_root, when provided, must resolve to ctx.project_root.
    - repository_fingerprint, when provided, must match the stored fingerprint.
    - No memory content is included in error responses.
    """
    strict_mode = os.environ.get(_STRICT_WORKSPACE_ENV, "0").strip() in ("1", "true", "yes")

    if workspace_root is None and repository_fingerprint is None:
        if strict_mode:
            return {
                "error": True,
                "error_code": "PROJECT_CONTEXT_UNVERIFIABLE",
                "error_detail": (
                    "Strict workspace isolation is enabled "
                    f"({_STRICT_WORKSPACE_ENV}=1). "
                    "The caller must supply workspace_root to confirm it is sending "
                    "this request to the correct Memory Engine server. "
                    "Set workspace_root=<absolute path to your project> in the tool call."
                ),
                "workspace_validation": "failed",
                "server_project": str(ctx.project_root),
            }
        # Compatibility mode: warn but allow
        _LOG.warning(
            "[workspace-isolation] No workspace_root provided. "
            "Cannot confirm request originates from project '%s'. "
            "Set MEMORY_ENGINE_STRICT_WORKSPACE=1 to require workspace validation.",
            ctx.project_root,
        )
        return None  # allowed with warning

    # Validate workspace_root if provided
    if workspace_root is not None:
        try:
            caller_root = Path(workspace_root).resolve()
        except (TypeError, ValueError) as exc:
            return {
                "error": True,
                "error_code": "PROJECT_CONTEXT_MISMATCH",
                "error_detail": f"workspace_root is not a valid path: {exc}",
                "workspace_validation": "failed",
            }
        server_root = ctx.project_root  # already resolved in ProjectContext.__init__
        if caller_root != server_root:
            return {
                "error": True,
                "error_code": "PROJECT_CONTEXT_MISMATCH",
                "error_detail": (
                    "The caller's workspace_root does not match this MCP server's "
                    "configured project root. This server is bound to a different project. "
                    "Ensure you are connecting to the Memory Engine server for your project."
                ),
                "workspace_validation": "failed",
                "server_project": str(server_root),
                # Note: do NOT include caller_root or any memory content in the response
                # to avoid cross-project information leakage.
            }

    # Validate repository_fingerprint if provided
    if repository_fingerprint is not None:
        stored = None
        if ctx.storage.fingerprint_path.exists():
            try:
                import json as _json
                raw = ctx.storage.fingerprint_path.read_text(encoding="utf-8").strip()
                try:
                    fp_data = _json.loads(raw)
                    stored = fp_data.get("fingerprint_hash") or fp_data.get("canonical_path")
                except (_json.JSONDecodeError, AttributeError):
                    stored = raw  # legacy plain-text format
            except Exception:
                stored = None

        if stored and stored != repository_fingerprint:
            # Check if caller sent the canonical path (v0 format) or a hash (v1 format)
            # For a path match: compare against stored canonical path
            try:
                import json as _json
                raw = ctx.storage.fingerprint_path.read_text(encoding="utf-8").strip()
                fp_data = _json.loads(raw) if raw.startswith("{") else None
                canonical = fp_data.get("canonical_path") if fp_data else raw
                if repository_fingerprint != canonical:
                    return {
                        "error": True,
                        "error_code": "REPOSITORY_FINGERPRINT_MISMATCH",
                        "error_detail": (
                            "The repository_fingerprint provided by the caller does not match "
                            "the fingerprint stored for this project. "
                            "This may indicate a copied or mismatched .memory-engine directory."
                        ),
                        "workspace_validation": "failed",
                    }
            except Exception:
                pass  # If we cannot read the fingerprint, allow (fail-open on fingerprint only)

    _LOG.debug(
        "[workspace-isolation] Workspace validated for project '%s'.", ctx.project_root
    )
    return None  # validation passed


# ---------------------------------------------------------------------------
# 1. retrieve_agent_context
# ---------------------------------------------------------------------------


def tool_retrieve_agent_context(
    ctx: ProjectContext,
    inp: RetrieveContextInput,
) -> dict[str, Any]:
    """Retrieve smallest relevant memory + knowledge before non-trivial work."""
    # Phase 14: workspace validation BEFORE any memory access
    ws_error = _validate_workspace(ctx, inp.workspace_root, inp.repository_fingerprint)
    if ws_error is not None:
        return ws_error

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

    # Phase 14: read current memory generation so the cache invalidates after writes
    state_mgr = ctx.get_state_manager()
    current_memory_generation = state_mgr.load().memory_revision

    session = ctx.get_session()
    try:
        svc = UnifiedContextRetrievalService(
            session,
            vector_index=ctx.get_vector_index(),
            cache=ctx.get_cache(),
            semantic_index=ctx.get_semantic_index(),
            project_root=str(ctx.project_root),
            # Task 5: bump the persisted memory_revision whenever a
            # source-validity transition is discovered during retrieval
            # (including the bounded cache-hit revalidation path), so the
            # NEXT retrieval call's cache key reflects the change.
            revision_hook=lambda: _bump_memory_revision(ctx),
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
            # Phase 14: ensures cache misses after memory writes
            memory_generation=current_memory_generation,
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
            semantic_backend=(
                mode_info.vector_backend if mode_info.semantic_status == "used"
                else "none"
            ),
            embedding_provider=mode_info.embedding_provider,
            embedding_model=mode_info.embedding_model,
            semantic_status=mode_info.semantic_status,
            # Issue 6: compact provenance envelope info, set once per
            # response rather than repeated per memory.
            repository_fingerprint=ctx.storage.short_repository_fingerprint(),
            project_id=ctx.get_project_id(),
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
    # Phase 14: workspace validation BEFORE any memory write
    ws_error = _validate_workspace(ctx, inp.workspace_root, inp.repository_fingerprint)
    if ws_error is not None:
        return ws_error

    bootstrap_report = ctx.ensure_bootstrapped()
    mode_info = ctx.get_mode_info()

    # Map verification_status string to enum
    try:
        ver_status = VerificationStatus(inp.verification_status)
    except ValueError:
        ver_status = VerificationStatus.unverified

    # Map outcome string to enum.
    # If the agent explicitly declared a verified status, trust it — do NOT let
    # keyword scanning of the outcome text override an explicit tests_passed /
    # build_success declaration.  Bug fix descriptions legitimately contain
    # words like "failed" or "error" when describing what was wrong, and
    # substring matching caused those tasks to be silently skipped.
    import re as _re
    _VERIFIED_STATUSES = {"tests_passed", "build_success"}
    task_outcome = TaskOutcome.completed  # default
    outcome_lower = inp.outcome.lower()
    def _word_match(text: str, words: tuple) -> bool:
        return any(_re.search(rf'\b{_re.escape(w)}\b', text) for w in words)
    if inp.verification_status not in _VERIFIED_STATUSES:
        # Only scan outcome text for failure signals when the agent has NOT
        # explicitly verified the task.
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
        # Phase 15 follow-up (Task 2/4): thread the validated ProjectContext
        # root through so source-backed candidates get real source_hash
        # evidence at write time. This is the already-validated server root
        # (ctx.project_root), never a raw caller-supplied path.
        svc = PostTaskService(session, project_root=ctx.project_root)
        # Phase 11: honour explicit task_intent from the agent
        explicit_intent: TaskIntent | None = None
        if inp.task_intent:
            try:
                explicit_intent = TaskIntent(inp.task_intent)
            except ValueError:
                explicit_intent = None

        # Issue 4: optional structured verification evidence. Omitted by
        # legacy callers — verification_status alone continues to drive
        # candidate confidence as before; internally this now also derives a
        # conservative VerificationEvidenceLevel (see
        # memory_engine.services.verification_evidence). Git branch/commit
        # already resolved above are reused here rather than making a fresh
        # Git call.
        verification_evidence = None
        if any((
            inp.evidence_target, inp.evidence_exit_code is not None,
            inp.evidence_output_digest, inp.evidence_observer, inp.evidence_external_ref,
        )):
            from memory_engine.models.domain import VerificationEvidence
            verification_evidence = VerificationEvidence(
                target=inp.evidence_target,
                exit_code=inp.evidence_exit_code,
                output_digest=inp.evidence_output_digest,
                observer=inp.evidence_observer,
                external_ref=inp.evidence_external_ref,
                source_branch=effective_branch,
                source_commit=effective_commit,
                working_tree_dirty=(git_ctx.working_tree_dirty if git_ctx.is_repository else None),
            )
        asserted_evidence_level = None
        if inp.asserted_evidence_level:
            from memory_engine.models.domain import VerificationEvidenceLevel
            try:
                asserted_evidence_level = VerificationEvidenceLevel(inp.asserted_evidence_level)
            except ValueError:
                asserted_evidence_level = None

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
            verification_evidence=verification_evidence,
            asserted_evidence_level=asserted_evidence_level,
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


def _semantic_suggestions(semantic_health: str) -> list[str]:
    if semantic_health == "disabled":
        return [
            "Semantic retrieval is disabled — semantic_similarity will always be 0.0. "
            "To enable: run `memory semantic status --enable --project-root /your/project` "
            "after installing `uv pip install 'memory-engine[semantic-transformers]'`."
        ]
    if semantic_health == "unavailable":
        return [
            "Semantic retrieval is configured but the provider or sqlite-vec backend is unavailable. "
            "Run `memory semantic doctor --project-root .` for details."
        ]
    return []


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

    # Phase 13: semantic retrieval health
    embedded_count = 0
    semantic_health = mode_info.semantic_status
    if mode_info.semantic_status == "used":
        semantic_health = "healthy"
        try:
            sem_index = ctx.get_semantic_index()
            if sem_index is not None:
                stats = sem_index.get_stats(ctx.get_project_id())
                embedded_count = stats.get("embedded_count", 0)
                if stats.get("orphan_count", 0) > 0:
                    semantic_health = "degraded"
        except Exception:
            semantic_health = "degraded"
    pending_count = max(0, chunks - embedded_count) if mode_info.semantic_enabled else 0
    semantic_backend = (
        mode_info.vector_backend if mode_info.semantic_status == "used" else "none"
    )

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
        # Phase 13 semantic fields
        semantic_enabled=mode_info.semantic_enabled,
        semantic_backend=semantic_backend,
        embedding_provider=mode_info.embedding_provider,
        embedding_model=mode_info.embedding_model,
        embedded_record_count=embedded_count,
        pending_embedding_count=pending_count,
        semantic_health=semantic_health,
        suggestions=_semantic_suggestions(semantic_health),
    ).model_dump()


# ---------------------------------------------------------------------------
# 6. refresh_project_knowledge
# ---------------------------------------------------------------------------


def tool_refresh_project_knowledge(ctx: ProjectContext) -> dict[str, Any]:
    """Trigger safe incremental rescan of changed sources."""
    return ctx.incremental_refresh()


# ---------------------------------------------------------------------------
# 7. seed_project_context
# ---------------------------------------------------------------------------


def tool_seed_project_context(
    ctx: ProjectContext,
    inp: SeedProjectInput,
) -> dict[str, Any]:
    """Seed initial memory nodes from structured project context.

    Call once when setting up a new project to eliminate the cold-start
    problem. Nodes are written directly to active status with full confidence
    because the source is an authoritative human description.
    """
    ctx.ensure_bootstrapped()
    session = ctx.get_session()
    try:
        from memory_engine.skills.seeding import ProjectSeedingService, SeedInput
        svc = ProjectSeedingService(session)
        result = svc.seed(SeedInput(
            project_id=uuid.UUID(ctx.get_project_id()),
            project_root=ctx.project_root,
            description=inp.description,
            constraints=inp.constraints,
            decisions=inp.decisions,
            tech_stack=inp.tech_stack,
            conventions=inp.conventions,
            skip_auto_extract=inp.skip_auto_extract,
        ))
        # Bump memory revision so subsequent retrieve calls see fresh data
        if result.nodes_created > 0:
            state_mgr = ctx.get_state_manager()
            state = state_mgr.load()
            state.bump_memory()
            state_mgr.save()
            ctx.get_cache().invalidate_project(ctx.get_project_id())

        return SeedProjectOutput(
            nodes_created=result.nodes_created,
            module_nodes=result.module_nodes,
            constraint_nodes=result.constraint_nodes,
            decision_nodes=result.decision_nodes,
            procedure_nodes=result.procedure_nodes,
            node_titles=result.node_titles,
            skipped_reason=result.skipped_reason,
        ).model_dump()
    finally:
        session.close()


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
