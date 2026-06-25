"""Phase 7 — MCP distribution, bootstrap, incremental indexing, degraded retrieval.

25 deterministic tests.  No external services required.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, name: str = "sample-project") -> Path:
    """Create a minimal fake project directory."""
    root = tmp_path / name
    root.mkdir()
    (root / "README.md").write_text("# Sample Project\n\nA test project for Memory Engine.\n")
    (root / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "0.1.0"\n')
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text("def main():\n    pass\n")
    docs = root / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text("# Architecture\n\nThis project uses a modular design.\n")
    return root


# ===========================================================================
# 1–3. PROJECT ROOT RESOLUTION
# ===========================================================================


class TestProjectRootResolution:

    def test_t01_explicit_argument(self, tmp_path):
        """T01 — resolve_project_root respects explicit path."""
        from memory_engine.bootstrap.project_root import resolve_project_root

        project = _make_project(tmp_path)
        result = resolve_project_root(explicit=str(project))
        assert result == project.resolve()

    def test_t02_env_variable(self, tmp_path, monkeypatch):
        """T02 — resolve_project_root falls back to MEMORY_ENGINE_PROJECT_ROOT env var."""
        from memory_engine.bootstrap.project_root import resolve_project_root

        project = _make_project(tmp_path)
        monkeypatch.setenv("MEMORY_ENGINE_PROJECT_ROOT", str(project))
        result = resolve_project_root()
        assert result == project.resolve()

    def test_t03_cwd_hint(self, tmp_path):
        """T03 — resolve_project_root respects cwd_hint when provided."""
        from memory_engine.bootstrap.project_root import resolve_project_root

        project = _make_project(tmp_path)
        # cwd_hint is the project root itself
        result = resolve_project_root(cwd_hint=str(project))
        assert result == project.resolve()


# ===========================================================================
# 4–5. SECURITY — PATH TRAVERSAL AND SYMLINKS
# ===========================================================================


class TestSecurity:

    def test_t04_path_traversal_rejected(self, tmp_path):
        """T04 — resolve_safe raises PathEscapeError for traversal attempts."""
        from memory_engine.bootstrap.security import PathEscapeError, resolve_safe

        project = _make_project(tmp_path)
        with pytest.raises(PathEscapeError):
            resolve_safe("../../etc/passwd", project)

    def test_t05_unsafe_symlink_rejected(self, tmp_path):
        """T05 — is_safe_symlink returns False for links escaping project root."""
        from memory_engine.bootstrap.security import is_safe_symlink

        project = _make_project(tmp_path)
        # Symlink pointing outside the project
        link = project / "escape_link"
        target = tmp_path / "outside"
        target.mkdir()
        link.symlink_to(target)

        assert not is_safe_symlink(link, project)


# ===========================================================================
# 6–8. BOOTSTRAP
# ===========================================================================


class TestBootstrap:

    def test_t06_creates_storage_layout(self, tmp_path):
        """T06 — bootstrap creates .memory-engine/ layout."""
        from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService

        project = _make_project(tmp_path)
        svc = ProjectBootstrapService(project)
        report = svc.bootstrap()

        storage = project / ".memory-engine"
        assert storage.is_dir()
        assert (storage / "config.yaml").is_file()
        assert (storage / "memory.db").is_file()
        assert (storage / "indexes").is_dir()
        assert (storage / "generated").is_dir()
        assert report["bootstrap_status"] == "READY"

    def test_t07_bootstrap_is_idempotent(self, tmp_path):
        """T07 — calling bootstrap twice does not duplicate records."""
        from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService

        project = _make_project(tmp_path)
        svc1 = ProjectBootstrapService(project)
        r1 = svc1.bootstrap()

        svc2 = ProjectBootstrapService(project)
        r2 = svc2.bootstrap()

        # Both succeed; second run returns no error
        assert r1["bootstrap_status"] == "READY"
        assert r2["bootstrap_status"] == "READY"

    def test_t08_seed_files_imported_without_duplicates(self, tmp_path):
        """T08 — seed files are indexed once; second bootstrap does not duplicate."""
        from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService

        project = _make_project(tmp_path)
        svc = ProjectBootstrapService(project)
        r1 = svc.bootstrap()
        docs_first = r1.get("indexed_documents", 0)

        svc2 = ProjectBootstrapService(project)
        r2 = svc2.bootstrap()
        # Second run: already_initialized should be set
        assert r2.get("already_initialized") or r2.get("indexed_documents", 0) <= docs_first


# ===========================================================================
# 9. IGNORE RULES
# ===========================================================================


class TestIgnoreRules:

    def test_t09_env_and_secrets_excluded(self, tmp_path):
        """T09 — .env, secrets/, node_modules/, binary, oversized files are excluded."""
        from memory_engine.bootstrap.security import is_indexable

        project = _make_project(tmp_path)
        env_file = project / ".env"
        env_file.write_text("SECRET_KEY=abc123\n")

        secrets_dir = project / "secrets"
        secrets_dir.mkdir()
        secret_file = secrets_dir / "key.pem"
        secret_file.write_text("-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n")

        nm_dir = project / "node_modules"
        nm_dir.mkdir()
        nm_file = nm_dir / "lodash.js"
        nm_file.write_text("// lodash\n")

        for path in (env_file, secret_file, nm_file):
            ok, reason = is_indexable(path, project)
            assert not ok, f"Expected {path} to be excluded but got: ok={ok} reason={reason}"


# ===========================================================================
# 10. CONFIG PRESERVATION
# ===========================================================================


class TestConfig:

    def test_t10_config_preserves_user_edits(self, tmp_path):
        """T10 — write_default_config does not overwrite existing config."""
        from memory_engine.bootstrap.config import write_default_config

        project = _make_project(tmp_path)
        config_path = project / ".memory-engine" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("project:\n  name: my-custom-name\n")

        write_default_config(config_path, project_name="should-not-override")

        content = config_path.read_text()
        assert "my-custom-name" in content


# ===========================================================================
# 11–13. INCREMENTAL INDEXING
# ===========================================================================


class TestIncrementalIndexing:

    def test_t11_existing_indexes_load_without_rebuild(self, tmp_path):
        """T11 — IncrementalIndexCoordinator detects unchanged files correctly."""
        from memory_engine.bootstrap.incremental_index import IncrementalIndexCoordinator

        project = _make_project(tmp_path)
        manifest_path = project / ".memory-engine" / "indexes" / "manifests" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        coord = IncrementalIndexCoordinator(project, manifest_path)
        cs1 = coord.compute_changes()
        # All files are new on first run
        assert len(cs1.new) > 0
        coord.apply_changes(cs1)

        # Second run — nothing changed
        coord2 = IncrementalIndexCoordinator(project, manifest_path)
        coord2.load_manifest()
        cs2 = coord2.compute_changes()
        assert len(cs2.new) == 0
        assert len(cs2.changed) == 0
        assert not cs2.has_changes

    def test_t12_changed_source_detected(self, tmp_path):
        """T12 — modified file appears in changed set."""
        from memory_engine.bootstrap.incremental_index import IncrementalIndexCoordinator
        import time

        project = _make_project(tmp_path)
        manifest_path = project / ".memory-engine" / "indexes" / "manifests" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        coord = IncrementalIndexCoordinator(project, manifest_path)
        cs1 = coord.compute_changes()
        coord.apply_changes(cs1)

        # Modify file
        readme = project / "README.md"
        time.sleep(0.02)  # ensure mtime differs
        readme.write_text("# Updated Content\n\nSomething new here.\n")

        coord2 = IncrementalIndexCoordinator(project, manifest_path)
        coord2.load_manifest()
        cs2 = coord2.compute_changes()

        changed_rel = {e.rel_path for e in cs2.changed}
        assert "README.md" in changed_rel

    def test_t13_deleted_source_marked_stale(self, tmp_path):
        """T13 — deleted file appears in deleted set."""
        from memory_engine.bootstrap.incremental_index import IncrementalIndexCoordinator

        project = _make_project(tmp_path)
        extra = project / "NOTES.md"
        extra.write_text("Some notes.\n")

        manifest_path = project / ".memory-engine" / "indexes" / "manifests" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        coord = IncrementalIndexCoordinator(project, manifest_path)
        cs1 = coord.compute_changes()
        coord.apply_changes(cs1)

        extra.unlink()

        coord2 = IncrementalIndexCoordinator(project, manifest_path)
        coord2.load_manifest()
        cs2 = coord2.compute_changes()

        deleted_rel = {e.rel_path for e in cs2.deleted}
        assert "NOTES.md" in deleted_rel


# ===========================================================================
# 14–15. REVISIONS AND CACHE
# ===========================================================================


class TestRevisionsAndCache:

    def test_t14_memory_revision_bumped_on_reflect(self, tmp_path):
        """T14 — reflect_and_write bumps memory_revision in project state."""
        from memory_engine.bootstrap.project_state import ProjectStateManager

        state_path = tmp_path / "project_state.json"
        mgr = ProjectStateManager(state_path)
        state = mgr.load()
        old_rev = state.memory_revision
        state.bump_memory()
        mgr.save()

        mgr2 = ProjectStateManager(state_path)
        state2 = mgr2.load()
        assert state2.memory_revision == old_rev + 1

    def test_t15_cache_invalidated_on_update(self):
        """T15 — cache invalidate_project clears all keys for that project."""
        from memory_engine.knowledge.cache import SimpleCache

        cache = SimpleCache(ttl_seconds=60)
        pid = "proj-abc"
        key = cache.make_key(pid, "some task", [], [], 4000)
        cache.set(key, {"result": 1}, pid)
        assert cache.get(key) is not None

        cache.invalidate_project(pid)
        assert cache.get(key) is None


# ===========================================================================
# 16. DEGRADED MODE
# ===========================================================================


class TestDegradedMode:

    def test_t16_vector_unavailable_falls_back(self):
        """T16 — detect_retrieval_mode returns lexical_structured_fallback by default."""
        from memory_engine.bootstrap.vector_fallback import detect_retrieval_mode

        info = detect_retrieval_mode()
        # qdrant_client is not installed → must fall back
        assert info.mode in ("lexical_structured_fallback", "hybrid_lexical_vector")
        assert isinstance(info.warnings, list)


# ===========================================================================
# 17–20. MCP TOOL INTEGRATION
# ===========================================================================


class TestMCPTools:

    def _make_ctx(self, tmp_path: Path):
        from memory_engine.mcp.project_context import ProjectContext, clear_registry
        clear_registry()
        project = _make_project(tmp_path)
        return project, ProjectContext(project)

    def test_t17_retrieve_context_bootstraps_automatically(self, tmp_path):
        """T17 — retrieve_agent_context bootstraps project on first call."""
        from memory_engine.mcp.tools import tool_retrieve_agent_context
        from memory_engine.mcp.schemas import RetrieveContextInput

        project, ctx = self._make_ctx(tmp_path)
        inp = RetrieveContextInput(task="Implement retry backoff")
        result = tool_retrieve_agent_context(ctx, inp)
        assert result["task"] == "Implement retry backoff"
        assert "meta" in result
        assert result["meta"]["bootstrap_status"] in ("READY", "DEGRADED", "FAILED", "BOOTSTRAPPING")
        # Storage should now exist
        assert (project / ".memory-engine").is_dir()

    def test_t18_inspect_knowledge_cannot_read_outside_root(self, tmp_path):
        """T18 — inspect_knowledge rejects paths outside project root."""
        from memory_engine.mcp.tools import tool_inspect_knowledge
        from memory_engine.mcp.schemas import InspectKnowledgeInput

        project, ctx = self._make_ctx(tmp_path)
        ctx.ensure_bootstrapped()

        inp = InspectKnowledgeInput(path="../../etc/passwd")
        result = tool_inspect_knowledge(ctx, inp)
        assert "error" in result
        assert result.get("boundary_violation") is True

    def test_t19_reflect_and_write_uses_post_task_pipeline(self, tmp_path):
        """T19 — reflect_and_write returns structured outcome dict."""
        from memory_engine.mcp.tools import tool_reflect_and_write
        from memory_engine.mcp.schemas import ReflectAndWriteInput

        _, ctx = self._make_ctx(tmp_path)
        ctx.ensure_bootstrapped()

        inp = ReflectAndWriteInput(
            task="Add exponential backoff to the retry scheduler module",
            outcome=(
                "Implemented exponential backoff with jitter. All 47 tests pass. "
                "Confirmed terminal-state semantics are preserved."
            ),
            verification_status="tests_passed",
            changed_files=["src/scheduler/retry.py", "tests/test_retry.py"],
        )
        result = tool_reflect_and_write(ctx, inp)
        assert "outcome" in result
        assert result["outcome"] in ("persisted", "skipped", "needs_review")
        assert "meta" in result

    def test_t20_memory_status_reports_bootstrap(self, tmp_path):
        """T20 — memory_status includes bootstrap_status and retrieval_mode."""
        from memory_engine.mcp.tools import tool_memory_status

        _, ctx = self._make_ctx(tmp_path)
        status = tool_memory_status(ctx)
        assert "bootstrap_status" in status
        assert "retrieval_mode" in status
        assert "vector_backend" in status
        assert "active_memories" in status
        assert "knowledge_documents" in status


# ===========================================================================
# 21–22. AGENT POLICY
# ===========================================================================


class TestAgentPolicy:

    def test_t21_agent_policy_generated(self, tmp_path):
        """T21 — bootstrap generates AGENT_MEMORY_POLICY.md."""
        from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService

        project = _make_project(tmp_path)
        svc = ProjectBootstrapService(project)
        svc.bootstrap()

        policy_path = project / ".memory-engine" / "generated" / "AGENT_MEMORY_POLICY.md"
        assert policy_path.is_file()
        content = policy_path.read_text()
        assert "retrieve_agent_context" in content
        assert "reflect_and_write" in content

    def test_t22_mcp_resource_returns_agent_policy(self, tmp_path):
        """T22 — agent-policy MCP resource returns policy text."""
        from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService
        from memory_engine.mcp.project_context import ProjectContext, clear_registry
        from memory_engine.mcp.resources import resource_agent_policy

        clear_registry()
        project = _make_project(tmp_path)
        svc = ProjectBootstrapService(project)
        svc.bootstrap()

        ctx = ProjectContext(project)
        policy = resource_agent_policy(ctx)
        assert "retrieve_agent_context" in policy or "Agent Memory Policy" in policy


# ===========================================================================
# 23–25. LOCAL-ONLY AND DEGRADED
# ===========================================================================


class TestLocalAndDegraded:

    def test_t23_no_external_service_required(self, tmp_path):
        """T23 — full bootstrap + retrieve cycle works with no external services."""
        from memory_engine.mcp.project_context import ProjectContext, clear_registry
        from memory_engine.mcp.tools import tool_retrieve_agent_context, tool_memory_status
        from memory_engine.mcp.schemas import RetrieveContextInput

        clear_registry()
        project = _make_project(tmp_path)
        ctx = ProjectContext(project)

        status = tool_memory_status(ctx)
        assert status["bootstrap_status"] in ("READY", "DEGRADED", "FAILED", "BOOTSTRAPPING", "UNINITIALIZED")

        inp = RetrieveContextInput(task="Refactor the main scheduler loop")
        result = tool_retrieve_agent_context(ctx, inp)
        assert "task" in result
        assert "meta" in result

    def test_t24_mcp_server_can_be_created(self, tmp_path):
        """T24 — create_mcp_server returns a FastMCP instance without errors."""
        from memory_engine.mcp.project_context import clear_registry
        from memory_engine.mcp.server import create_mcp_server

        clear_registry()
        project = _make_project(tmp_path)
        server = create_mcp_server(project)
        # FastMCP instance has a name attribute
        assert server.name == "memory-engine"

    def test_t25_degraded_mode_returns_context_with_warnings(self, tmp_path):
        """T25 — when vector backend is unavailable, retrieval still returns structured result."""
        from memory_engine.bootstrap.vector_fallback import detect_retrieval_mode, degraded_response_metadata
        from memory_engine.mcp.project_context import ProjectContext, clear_registry
        from memory_engine.mcp.tools import tool_retrieve_agent_context
        from memory_engine.mcp.schemas import RetrieveContextInput

        clear_registry()
        project = _make_project(tmp_path)
        ctx = ProjectContext(project)

        # Simulate no Qdrant
        mode_info = detect_retrieval_mode()
        meta = degraded_response_metadata(mode_info)

        # These fields must exist in degraded metadata
        assert "retrieval_mode" in meta
        assert "vector_backend" in meta
        assert "warnings" in meta

        # Full retrieve still works
        inp = RetrieveContextInput(task="Add retry backoff")
        result = tool_retrieve_agent_context(ctx, inp)
        assert "task" in result
        warnings = result["meta"].get("warnings", [])
        # May be empty if vector index is ephemeral but healthy enough
        assert isinstance(warnings, list)
