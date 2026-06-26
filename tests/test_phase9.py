"""Phase 9 — Git-Aware Incremental Synchronization and Branch-Aware Memory.

40 deterministic tests:
 - GitContext / GitRename dataclasses
 - GitContextResolver (using temp git repos)
 - git_security: allowed / blocked commands
 - Schema migration (ADD COLUMN idempotency)
 - SimpleCache branch-aware key isolation
 - DeterministicRanker branch signals
 - ProjectState Phase 9 fields
 - RelationType Phase 9 values
 - UnifiedRetrievalRequest branch fields
 - RetrievalMeta / MemoryStatusOutput Phase 9 fields
 - IncrementalIndexCoordinator Phase 9 git-aware method
 - MCP schemas round-trip
 - Resource helpers (non-DB)
 - Integration: ProjectContext.get_git_context()
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from memory_engine.bootstrap.incremental_index import IncrementalIndexCoordinator
from memory_engine.bootstrap.project_state import ProjectState
from memory_engine.db.init_db import apply_schema_migrations
from memory_engine.knowledge.cache import SimpleCache
from memory_engine.models.domain import RelationType
from memory_engine.models.knowledge_domain import UnifiedRetrievalRequest
from memory_engine.mcp.schemas import (
    MemoryStatusOutput,
    RetrievalMeta,
    RetrieveContextInput,
    ReflectAndWriteInput,
)
from memory_engine.runtime.git import GitContext, GitContextResolver
from memory_engine.runtime.git.git_context import GitRename
from memory_engine.runtime.git.git_security import (
    GitSecurityError,
    is_git_available,
    run_git,
)
from memory_engine.skills.ranker import DeterministicRanker
from memory_engine.models.domain import MemoryNode, MemoryKind, TaskIntent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_git_repo(branch: str = "main") -> Path:
    """Create a throwaway git repo with one commit.  Returns the repo path."""
    tmpdir = Path(tempfile.mkdtemp())
    _git(tmpdir, ["init", "-b", branch])
    _git(tmpdir, ["config", "user.email", "test@example.com"])
    _git(tmpdir, ["config", "user.name", "Test"])
    readme = tmpdir / "README.md"
    readme.write_text("# test")
    _git(tmpdir, ["add", "README.md"])
    _git(tmpdir, ["commit", "-m", "init", "--allow-empty-message"])
    return tmpdir


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )


def _node(
    branch_name: str | None = None,
    branch_scope: str | None = "global",
    source_path: str | None = None,
    importance: float = 0.5,
    confidence: float = 0.8,
) -> MemoryNode:
    n = MemoryNode(
        id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        title="Test node",
        summary="A test memory node summary with some content",
        kind=MemoryKind.module,
        depth=0,
        tags=["python"],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        status="active",
        confidence=confidence,
        importance=importance,
    )
    n.branch_name = branch_name
    n.branch_scope = branch_scope
    n.source_path = source_path
    return n


# ===========================================================================
# 1. GitRename
# ===========================================================================


def test_git_rename_fields():
    r = GitRename(old_path="a.py", new_path="b.py", similarity_score=95.0)
    assert r.old_path == "a.py"
    assert r.new_path == "b.py"
    assert r.similarity_score == 95.0


def test_git_rename_no_score():
    r = GitRename(old_path="x", new_path="y")
    assert r.similarity_score is None


# ===========================================================================
# 2. GitContext dataclass
# ===========================================================================


def test_git_context_unavailable_factory():
    ctx = GitContext.unavailable("/tmp/x", "git not found")
    assert not ctx.git_available
    assert not ctx.is_repository
    assert "git not found" in ctx.warnings


def test_git_context_non_repository_factory():
    ctx = GitContext.non_repository("/tmp/x")
    assert ctx.git_available
    assert not ctx.is_repository
    assert "not a Git repository" in ctx.warnings[0]


def test_git_context_is_feature_branch():
    ctx = GitContext(
        repository_root="/x", git_available=True, is_repository=True,
        current_branch="feat/some-feature",
    )
    assert ctx.is_feature_branch()


def test_git_context_is_mainline():
    for branch in ("main", "master", "develop", "trunk"):
        ctx = GitContext(
            repository_root="/x", git_available=True, is_repository=True,
            current_branch=branch,
        )
        assert ctx.is_mainline(), f"{branch} should be mainline"
        assert not ctx.is_feature_branch()


def test_git_context_all_changed_paths():
    rename = GitRename(old_path="old.py", new_path="new.py")
    ctx = GitContext(
        repository_root="/x", git_available=True, is_repository=True,
        staged_files=("a.py",),
        modified_files=("b.py",),
        deleted_files=("c.py",),
        renamed_files=(rename,),
    )
    paths = ctx.all_changed_paths()
    assert "a.py" in paths
    assert "b.py" in paths
    assert "c.py" in paths
    assert "new.py" in paths


def test_git_context_to_dict_no_credentials():
    ctx = GitContext(
        repository_root="/x", git_available=True, is_repository=True,
        current_branch="main", head_commit="deadbeef",
    )
    d = ctx.to_dict()
    # Must not contain remote URL or user identity
    raw = str(d)
    assert "github.com" not in raw
    assert "user.email" not in raw
    assert d["current_branch"] == "main"
    assert d["head_commit"] == "deadbeef"


# ===========================================================================
# 3. GitContextResolver on real temp repos
# ===========================================================================


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_detects_main_branch():
    repo = _make_temp_git_repo(branch="main")
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    assert ctx.is_repository
    assert ctx.current_branch == "main"
    assert ctx.head_commit is not None
    assert len(ctx.head_commit) == 8  # short SHA


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_detects_feature_branch():
    repo = _make_temp_git_repo(branch="main")
    _git(repo, ["checkout", "-b", "feat/retry-v2"])
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    assert ctx.current_branch == "feat/retry-v2"
    assert ctx.is_feature_branch()


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_detects_base_branch():
    repo = _make_temp_git_repo(branch="main")
    _git(repo, ["checkout", "-b", "feature/test"])
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    assert ctx.base_branch == "main"


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_detects_modified_files():
    repo = _make_temp_git_repo(branch="main")
    (repo / "new_file.py").write_text("print('hello')")
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    assert ctx.working_tree_dirty
    # new_file.py is untracked
    assert "new_file.py" in ctx.untracked_files


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_detects_staged_file():
    repo = _make_temp_git_repo(branch="main")
    f = repo / "staged.py"
    f.write_text("# staged")
    _git(repo, ["add", "staged.py"])
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    assert "staged.py" in ctx.staged_files


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_on_non_git_dir():
    tmpdir = Path(tempfile.mkdtemp())
    resolver = GitContextResolver(tmpdir)
    ctx = resolver.resolve()
    assert not ctx.is_repository
    assert ctx.git_available


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_resolver_never_returns_remote_url():
    repo = _make_temp_git_repo(branch="main")
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    d = ctx.to_dict()
    assert "github.com" not in str(d)
    assert "origin" not in str(d)


# ===========================================================================
# 4. git_security module
# ===========================================================================


def test_git_security_blocks_destructive_commands():
    from pathlib import Path
    cwd = Path("/tmp")
    blocked = ["commit", "reset", "push", "clean", "checkout", "merge", "rebase", "fetch"]
    for cmd in blocked:
        with pytest.raises(GitSecurityError):
            run_git([cmd], cwd=cwd)


def test_git_security_allows_status():
    """run_git allows 'status' command (may fail if not a git repo, but not a security error)."""
    cwd = Path("/tmp")
    try:
        stdout, stderr, rc = run_git(["status", "--porcelain=v1"], cwd=cwd)
        # Either succeeds or fails with non-zero return — but no SecurityError
    except GitSecurityError:
        pytest.fail("status should be allowed")


def test_git_security_rejects_unknown_command():
    cwd = Path("/tmp")
    # "config" is in _BLOCKED_COMMANDS — "Blocked" error message
    with pytest.raises(GitSecurityError):
        run_git(["config"], cwd=cwd)
    # "describe" is not in either list — "not in the allowed list" error
    with pytest.raises(GitSecurityError, match="not in the allowed list"):
        run_git(["describe"], cwd=cwd)


# ===========================================================================
# 5. Schema migration idempotency
# ===========================================================================


def test_apply_schema_migrations_idempotent():
    """Apply schema migrations twice — should not raise."""
    engine = create_engine("sqlite:///:memory:")
    from memory_engine.models.orm import Base
    import memory_engine.models.knowledge_orm  # noqa: F401
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        apply_schema_migrations(conn)
        apply_schema_migrations(conn)   # second call must not raise
        conn.commit()


def test_apply_schema_migrations_adds_branch_columns():
    engine = create_engine("sqlite:///:memory:")
    from memory_engine.models.orm import Base
    import memory_engine.models.knowledge_orm  # noqa: F401
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        apply_schema_migrations(conn)
        conn.commit()
    # Verify branch_name exists on memory_nodes via PRAGMA
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(memory_nodes)")).fetchall()
        col_names = [row[1] for row in result]
        assert "branch_name" in col_names
        assert "branch_scope" in col_names
        assert "commit_sha" in col_names


# ===========================================================================
# 6. SimpleCache branch-aware key isolation
# ===========================================================================


def test_cache_key_differs_by_branch():
    k1 = SimpleCache.make_key("p", "q", [], [], 1000, current_branch="main")
    k2 = SimpleCache.make_key("p", "q", [], [], 1000, current_branch="feat/x")
    assert k1 != k2


def test_cache_key_differs_by_commit():
    k1 = SimpleCache.make_key("p", "q", [], [], 1000, head_commit="aaa")
    k2 = SimpleCache.make_key("p", "q", [], [], 1000, head_commit="bbb")
    assert k1 != k2


def test_cache_key_dirty_flag():
    k1 = SimpleCache.make_key("p", "q", [], [], 1000, working_tree_dirty=False)
    k2 = SimpleCache.make_key("p", "q", [], [], 1000, working_tree_dirty=True)
    assert k1 != k2


def test_cache_key_backward_compat():
    """Old callers without branch args must still get a valid key."""
    k = SimpleCache.make_key("p", "q", ["f.py"], ["Foo"], 2000)
    assert len(k) == 32


# ===========================================================================
# 7. DeterministicRanker branch-aware scoring
# ===========================================================================


def test_ranker_exact_branch_match_highest():
    ranker = DeterministicRanker()
    same_branch = _node(branch_name="feat/retry", branch_scope="current_branch")
    diff_branch = _node(branch_name="feat/other", branch_scope="current_branch")

    s_same = ranker.score(
        same_branch, task="test task", intent=TaskIntent.feature_implementation,
        current_files=[], current_symbols=[], current_branch="feat/retry",
    )
    s_diff = ranker.score(
        diff_branch, task="test task", intent=TaskIntent.feature_implementation,
        current_files=[], current_symbols=[], current_branch="feat/retry",
    )
    assert s_same.score > s_diff.score


def test_ranker_global_node_score():
    ranker = DeterministicRanker()
    node = _node(branch_name=None, branch_scope="global")
    scored = ranker.score(
        node, task="test task", intent=TaskIntent.feature_implementation,
        current_files=[], current_symbols=[], current_branch="feat/x",
    )
    assert 0.0 < scored.score <= 1.0


def test_ranker_branch_signals_in_breakdown():
    ranker = DeterministicRanker()
    node = _node(branch_name="feat/x", branch_scope="current_branch")
    scored = ranker.score(
        node, task="test", intent=TaskIntent.bug_fix,
        current_files=[], current_symbols=[], current_branch="feat/x",
    )
    assert "branch_affinity" in scored.score_breakdown
    assert "revision_validity" in scored.score_breakdown
    assert "branch_scope_priority" in scored.score_breakdown


def test_ranker_backward_compat_no_branch():
    """When current_branch=None, Phase 9 signals are not applied."""
    ranker = DeterministicRanker()
    node = _node()
    scored = ranker.score(
        node, task="test", intent=TaskIntent.bug_fix,
        current_files=[], current_symbols=[],
    )
    # No branch signals in breakdown — only base_score based signals
    assert "branch_affinity" not in scored.score_breakdown


def test_ranker_working_tree_source_match():
    ranker = DeterministicRanker()
    node = _node(branch_name="feat/x", source_path="src/retry.py")
    scored = ranker.score(
        node, task="test", intent=TaskIntent.bug_fix,
        current_files=[], current_symbols=[],
        current_branch="feat/x",
        modified_files=["src/retry.py"],
    )
    assert scored.score_breakdown["working_tree_source_match"] == 1.0


def test_ranker_mainline_scope_affinity():
    ranker = DeterministicRanker()
    mainline_node = _node(branch_name="main", branch_scope="mainline")
    scored = ranker.score(
        mainline_node, task="test", intent=TaskIntent.architecture_review,
        current_files=[], current_symbols=[], current_branch="feat/x",
    )
    assert scored.score_breakdown["branch_affinity"] == 0.5


# ===========================================================================
# 8. ProjectState Phase 9 fields
# ===========================================================================


def test_project_state_default_git_fields():
    s = ProjectState()
    assert s.current_branch is None
    assert s.head_commit is None
    assert s.last_git_sync_at == ""
    assert s.synchronization_status == "idle"
    assert not s.branch_aware_retrieval_enabled


def test_project_state_git_fields_serializable():
    s = ProjectState(
        current_branch="feat/x", head_commit="deadbeef",
        last_git_sync_at="2026-06-26T00:00:00+00:00",
    )
    data = s.model_dump()
    assert data["current_branch"] == "feat/x"
    assert data["head_commit"] == "deadbeef"


def test_project_state_load_from_old_json():
    """Deserializing old JSON (no Phase 9 fields) must not raise."""
    import json
    old_json = json.dumps({
        "project_revision": "abc",
        "index_revision": 1,
        "bootstrap_status": "READY",
    })
    s = ProjectState.model_validate(json.loads(old_json))
    assert s.current_branch is None


# ===========================================================================
# 9. RelationType Phase 9 values
# ===========================================================================


def test_relation_type_phase9_values():
    assert RelationType.derived_from_branch == "derived_from_branch"
    assert RelationType.inherited_from_mainline == "inherited_from_mainline"
    assert RelationType.promoted_to_mainline == "promoted_to_mainline"
    assert RelationType.invalidated_by_branch_change == "invalidated_by_branch_change"
    assert RelationType.renamed_source == "renamed_source"


# ===========================================================================
# 10. UnifiedRetrievalRequest Phase 9 fields
# ===========================================================================


def test_unified_retrieval_request_branch_fields():
    req = UnifiedRetrievalRequest(
        project_id=uuid.uuid4(),
        task="implement retry logic",
        current_branch="feat/retry-v2",
        head_commit="abc12345",
        modified_files=["src/retry.py"],
    )
    assert req.current_branch == "feat/retry-v2"
    assert req.head_commit == "abc12345"
    assert "src/retry.py" in req.modified_files


def test_unified_retrieval_request_backward_compat():
    req = UnifiedRetrievalRequest(project_id=uuid.uuid4(), task="test")
    assert req.current_branch is None
    assert req.head_commit is None
    assert req.modified_files == []


# ===========================================================================
# 11. MCP schema Phase 9 fields
# ===========================================================================


def test_retrieval_meta_git_fields():
    meta = RetrievalMeta(
        current_branch="feat/x",
        head_commit="deadbeef",
        branch_aware_ranking=True,
        git_available=True,
        is_repository=True,
    )
    d = meta.model_dump()
    assert d["current_branch"] == "feat/x"
    assert d["branch_aware_ranking"] is True


def test_memory_status_output_git_fields():
    out = MemoryStatusOutput(
        project_name="test",
        project_root="/tmp/test",
        bootstrap_status="READY",
        retrieval_mode="lexical",
        vector_backend="ephemeral",
        current_branch="main",
        head_commit="abc",
        git_available=True,
        is_repository=True,
    )
    d = out.model_dump()
    assert d["current_branch"] == "main"
    assert d["git_available"] is True


def test_retrieve_context_input_branch_fields():
    inp = RetrieveContextInput(
        task="test task",
        current_branch="feat/x",
        head_commit="abc12345",
    )
    assert inp.current_branch == "feat/x"


def test_reflect_and_write_input_branch_fields():
    inp = ReflectAndWriteInput(
        task="implement retry",
        outcome="done",
        current_branch="feat/retry",
        head_commit="abc12345",
    )
    assert inp.current_branch == "feat/retry"


# ===========================================================================
# 12. IncrementalIndexCoordinator Phase 9
# ===========================================================================


def test_incremental_index_compute_changes_git_aware_no_context():
    """Without GitContext, falls back to standard compute_changes."""
    tmpdir = Path(tempfile.mkdtemp())
    manifest_path = tmpdir / "manifest.json"
    coordinator = IncrementalIndexCoordinator(tmpdir, manifest_path)
    cs = coordinator.compute_changes_git_aware(None)
    # Should work without errors (returns a ChangeSet)
    assert hasattr(cs, "new")
    assert hasattr(cs, "changed")
    assert hasattr(cs, "deleted")


@pytest.mark.skipif(not is_git_available(), reason="git not installed")
def test_incremental_index_compute_changes_git_aware_clean_tree():
    """Clean working tree → fallback to full scan (no issues)."""
    repo = _make_temp_git_repo(branch="main")
    manifest_path = repo / "manifest.json"
    coordinator = IncrementalIndexCoordinator(repo, manifest_path)
    resolver = GitContextResolver(repo)
    ctx = resolver.resolve()
    cs = coordinator.compute_changes_git_aware(ctx)
    assert hasattr(cs, "new")


# ===========================================================================
# 13. ProjectContext.get_git_context()
# ===========================================================================


def test_project_context_get_git_context():
    from memory_engine.mcp.project_context import ProjectContext
    # Derive repo root from this file's location — portable across OS/CI
    repo_root = Path(__file__).parent.parent
    ctx = ProjectContext(repo_root)
    git_ctx = ctx.get_git_context()
    assert git_ctx.is_repository
    # CI uses detached HEAD so current_branch may be None; head_commit is always present
    assert git_ctx.head_commit is not None


def test_project_context_get_git_context_refresh():
    from memory_engine.mcp.project_context import ProjectContext
    repo_root = Path(__file__).parent.parent
    ctx = ProjectContext(repo_root)
    git_ctx1 = ctx.get_git_context()
    git_ctx2 = ctx.get_git_context(refresh=True)
    # Both should be valid — second is a fresh resolve
    assert git_ctx1.is_repository
    assert git_ctx2.is_repository
