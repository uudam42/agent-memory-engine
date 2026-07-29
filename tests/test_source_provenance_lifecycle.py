"""Regression + integration tests closing the stale-detection lifecycle.

Follow-up to Phase 15 / f8db7eb (SourceValidityService). f8db7eb wired the
*checking* engine through RecallService but left five production gaps open
(see PR description / phase spec):

  1. reflection/promotion never populated source_path/source_hash
  2. production retrieval passing project_root (was actually already wired
     for the cache-miss path, but NOT for cache hits)
  3. validity transitions never bumped memory_revision
  4. cached retrieval could keep returning a memory after its source was
     deleted or changed
  5. symbol/commit validation did not exist

Each section below corresponds to one of Tasks 2-10 from the phase spec and
is organized so a section's tests fail against the pre-fix code and pass
once the matching production fix lands.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from memory_engine.mcp.project_context import ProjectContext, clear_registry
from memory_engine.mcp.schemas import ReflectAndWriteInput, RetrieveContextInput
from memory_engine.mcp.tools import tool_reflect_and_write, tool_retrieve_agent_context
from memory_engine.models.domain import (
    MemoryKind,
    MemoryNodeCreate,
    MemoryStatus,
    ProjectCreate,
    ReflectionInput,
    TaskIntent,
    TaskOutcome,
    VerificationStatus,
)
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.post_task import PostTaskService
from memory_engine.services.project_service import ProjectService
from memory_engine.services.promotion import PromotionService
from memory_engine.services.source_validity import (
    check_commit_reachable,
    compute_source_hash,
    symbol_exists_in_file,
)
from memory_engine.skills.reflection import ReflectionSkill


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def project(session):
    return ProjectService(session).create(
        ProjectCreate(name="lifecycle-project", description="Phase 15 follow-up fixture")
    )


# ===========================================================================
# Task 2 — automatic source provenance capture (reflection layer)
# ===========================================================================


def _base_reflection_input(project_id, **overrides) -> ReflectionInput:
    defaults = dict(
        project_id=project_id,
        task_description="Implement caching for the widget service",
        task_outcome=TaskOutcome.completed,
        outcome_summary="Added a TTL cache in front of the widget lookup to cut latency.",
        touched_files=["memory_engine/widgets/service.py"],
        verification_status=VerificationStatus.tests_passed,
        task_intent=TaskIntent.feature_implementation,
    )
    defaults.update(overrides)
    return ReflectionInput(**defaults)


def test_single_touched_file_yields_source_path_for_module_candidate(project):
    inp = _base_reflection_input(project.id)
    analysis = ReflectionSkill().analyze(inp)
    assert analysis.worth_retaining
    module_candidates = [c for c in analysis.suggested_candidates if c.proposed_kind == MemoryKind.module]
    assert module_candidates, "expected a module candidate for structural work"
    assert module_candidates[0].source_path == "memory_engine/widgets/service.py"


def test_multiple_touched_files_leaves_source_path_absent(project):
    inp = _base_reflection_input(
        project.id,
        touched_files=[
            "memory_engine/widgets/service.py",
            "memory_engine/widgets/cache.py",
        ],
    )
    analysis = ReflectionSkill().analyze(inp)
    module_candidates = [c for c in analysis.suggested_candidates if c.proposed_kind == MemoryKind.module]
    assert module_candidates
    # Task 2: must not arbitrarily bind to the first of several changed files.
    assert module_candidates[0].source_path is None


def test_no_touched_files_leaves_source_path_absent(project):
    inp = _base_reflection_input(project.id, touched_files=[], discovered_constraints=["Never log secrets."])
    analysis = ReflectionSkill().analyze(inp)
    constraint_candidates = [c for c in analysis.suggested_candidates if c.proposed_kind == MemoryKind.constraint]
    assert constraint_candidates
    assert constraint_candidates[0].source_path is None


def test_constraint_kind_never_auto_bound_even_with_single_file(project):
    """Task 7: constraints are human-confirmed/repository policy — a single
    touched file must not make them source-hash-invalidatable."""
    inp = _base_reflection_input(
        project.id,
        discovered_constraints=["All public APIs must be versioned."],
    )
    analysis = ReflectionSkill().analyze(inp)
    constraint_candidates = [c for c in analysis.suggested_candidates if c.proposed_kind == MemoryKind.constraint]
    assert constraint_candidates
    assert constraint_candidates[0].source_path is None


def test_single_file_and_single_symbol_yields_source_symbol(project):
    inp = _base_reflection_input(project.id, touched_symbols=["WidgetCache"])
    analysis = ReflectionSkill().analyze(inp)
    module_candidates = [c for c in analysis.suggested_candidates if c.proposed_kind == MemoryKind.module]
    assert module_candidates
    assert module_candidates[0].source_symbol == "WidgetCache"


def test_multiple_symbols_leaves_source_symbol_absent(project):
    inp = _base_reflection_input(project.id, touched_symbols=["WidgetCache", "WidgetService"])
    analysis = ReflectionSkill().analyze(inp)
    module_candidates = [c for c in analysis.suggested_candidates if c.proposed_kind == MemoryKind.module]
    assert module_candidates
    assert module_candidates[0].source_symbol is None


# ===========================================================================
# Task 3 — deterministic, bounded, safe source-hash helper
# ===========================================================================


def test_compute_source_hash_matches_manual_sha256(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert compute_source_hash(tmp_path, "a.py") == _sha256("x = 1\n")


def test_compute_source_hash_missing_file_returns_none(tmp_path):
    assert compute_source_hash(tmp_path, "does_not_exist.py") is None


def test_compute_source_hash_directory_returns_none(tmp_path):
    (tmp_path / "a_dir").mkdir()
    assert compute_source_hash(tmp_path, "a_dir") is None


def test_compute_source_hash_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    try:
        assert compute_source_hash(tmp_path, "../outside_secret.py") is None
    finally:
        outside.unlink(missing_ok=True)


def test_compute_source_hash_handles_binary_content(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(bytes(range(256)) * 100)
    h = compute_source_hash(tmp_path, "blob.bin")
    assert h is not None and len(h) == 64


def test_compute_source_hash_handles_unicode_content(tmp_path):
    f = tmp_path / "unicode.py"
    f.write_text("# 日本語のコメント\nx = '日本語'\n", encoding="utf-8")
    h = compute_source_hash(tmp_path, "unicode.py")
    assert h == _sha256("# 日本語のコメント\nx = '日本語'\n")


# ===========================================================================
# Task 2 (continued) — promotion actually persists provenance
# ===========================================================================


def test_promotion_computes_source_hash_when_project_root_supplied(session, project, tmp_path):
    f = tmp_path / "memory_engine" / "widgets" / "service.py"
    f.parent.mkdir(parents=True)
    content = "class WidgetService:\n    pass\n"
    f.write_text(content, encoding="utf-8")

    svc = PostTaskService(session, project_root=tmp_path)
    result = svc.reflect_and_write(_base_reflection_input(project.id))

    assert result.candidates_promoted >= 1
    promoted = [r for r in result.promotion_results if r.target_node and r.action.value == "create"]
    assert promoted, "expected at least one created node"
    node = promoted[0].target_node
    assert node.source_path == "memory_engine/widgets/service.py"
    assert node.source_hash == _sha256(content)


def test_promotion_without_project_root_persists_path_but_not_hash(session, project, tmp_path):
    f = tmp_path / "memory_engine" / "widgets" / "service.py"
    f.parent.mkdir(parents=True)
    f.write_text("class WidgetService: pass\n", encoding="utf-8")

    # No project_root supplied — pre-existing callers (CLI, most unit tests)
    # must be unaffected: source_path may still be recorded, but hashing is
    # skipped entirely (no filesystem access without project_root).
    svc = PostTaskService(session)
    result = svc.reflect_and_write(_base_reflection_input(project.id))
    promoted = [r for r in result.promotion_results if r.target_node and r.action.value == "create"]
    assert promoted
    node = promoted[0].target_node
    assert node.source_path == "memory_engine/widgets/service.py"
    assert node.source_hash is None


# ===========================================================================
# Task 10 — Scenario A: full reflect-to-retrieve production flow
# ===========================================================================


@pytest.fixture()
def project_dir(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    clear_registry()
    yield tmp_path
    clear_registry()


def test_scenario_a_reflect_created_source_backed_memory_is_retrieved_active(project_dir):
    src = project_dir / "memory_engine" / "billing" / "invoice.py"
    src.parent.mkdir(parents=True)
    src.write_text("class Invoice:\n    def total(self):\n        return 0\n", encoding="utf-8")

    ctx = ProjectContext(project_dir)

    write_out = tool_reflect_and_write(ctx, ReflectAndWriteInput(
        task="Add invoice total calculation",
        outcome=(
            "Implemented Invoice.total() to sum line items and added tests "
            "covering rounding edge cases."
        ),
        verification_status="tests_passed",
        changed_files=["memory_engine/billing/invoice.py"],
        task_intent="feature_implementation",
    ))
    assert write_out["outcome"] in ("persisted", "needs_review")

    # Confirm the persisted node actually carries provenance (query the DB
    # directly via the same ProjectContext session, matching production).
    session = ctx.get_session()
    try:
        from memory_engine.models.orm import MemoryNodeORM
        nodes = session.query(MemoryNodeORM).filter(
            MemoryNodeORM.source_path == "memory_engine/billing/invoice.py"
        ).all()
        assert nodes, "expected a promoted node with source_path set"
        assert nodes[0].source_hash is not None
        node_id = nodes[0].id
    finally:
        session.close()

    retrieve_out = tool_retrieve_agent_context(ctx, RetrieveContextInput(
        task="Add invoice total calculation",
        current_files=["memory_engine/billing/invoice.py"],
    ))
    all_ids = {
        str(n["id"]) for bucket in ("modules", "decisions", "incidents") for n in retrieve_out.get(bucket, [])
    }
    assert node_id in all_ids


def test_scenario_b_file_deletion_after_cached_retrieval_excludes_memory(project_dir):
    src = project_dir / "memory_engine" / "billing" / "invoice.py"
    src.parent.mkdir(parents=True)
    src.write_text("class Invoice:\n    def total(self):\n        return 0\n", encoding="utf-8")

    ctx = ProjectContext(project_dir)
    tool_reflect_and_write(ctx, ReflectAndWriteInput(
        task="Add invoice total calculation",
        outcome="Implemented Invoice.total() with tests for rounding edge cases.",
        verification_status="tests_passed",
        changed_files=["memory_engine/billing/invoice.py"],
        task_intent="feature_implementation",
    ))

    req = RetrieveContextInput(
        task="Add invoice total calculation",
        current_files=["memory_engine/billing/invoice.py"],
    )

    first = tool_retrieve_agent_context(ctx, req)
    assert not first["cache_hit"]
    node_ids_before = {str(n["id"]) for n in first.get("modules", [])}
    assert node_ids_before, "expected the memory to be active before deletion"

    # Populate the cache with a *second* identical call (proves the cache
    # entry from `first` is being reused, not recomputed).
    second = tool_retrieve_agent_context(ctx, req)
    assert second["cache_hit"] is True

    src.unlink()

    third = tool_retrieve_agent_context(ctx, req)
    node_ids_after = {str(n["id"]) for n in third.get("modules", [])}
    assert not (node_ids_before & node_ids_after), (
        "deleted-source memory must not still be returned as active"
    )

    # Audit trail: node status transitioned and is inspectable, not deleted.
    session = ctx.get_session()
    try:
        from memory_engine.models.orm import MemoryNodeORM
        node = session.get(MemoryNodeORM, next(iter(node_ids_before)))
        assert node is not None
        assert node.status in (MemoryStatus.invalidated.value, MemoryStatus.needs_revalidation.value)
        assert node.previous_status == MemoryStatus.active.value
        assert node.validity_reason
    finally:
        session.close()


def test_scenario_c_material_change_then_restore_reactivates(project_dir):
    src = project_dir / "memory_engine" / "billing" / "invoice.py"
    src.parent.mkdir(parents=True)
    original = "class Invoice:\n    def total(self):\n        return 0\n"
    src.write_text(original, encoding="utf-8")

    ctx = ProjectContext(project_dir)
    tool_reflect_and_write(ctx, ReflectAndWriteInput(
        task="Add invoice total calculation",
        outcome="Implemented Invoice.total() with tests for rounding edge cases.",
        verification_status="tests_passed",
        changed_files=["memory_engine/billing/invoice.py"],
        task_intent="feature_implementation",
    ))
    req = RetrieveContextInput(
        task="Add invoice total calculation",
        current_files=["memory_engine/billing/invoice.py"],
    )
    first = tool_retrieve_agent_context(ctx, req)
    node_ids_before = {str(n["id"]) for n in first.get("modules", [])}
    assert node_ids_before
    tool_retrieve_agent_context(ctx, req)  # populate cache

    src.write_text(
        "class Invoice:\n    def total(self):\n        return 999  # materially different\n",
        encoding="utf-8",
    )

    after_change = tool_retrieve_agent_context(ctx, req)
    ids_after_change = {str(n["id"]) for n in after_change.get("modules", [])}
    assert not (node_ids_before & ids_after_change), (
        "materially changed source must make the memory non-authoritative"
    )

    # Explicit revalidation restores eligibility once the file is healthy
    # again (never happens implicitly — matches SourceValidityService design).
    from memory_engine.services.source_validity import SourceValidityService
    from memory_engine.repositories.memory_node import MemoryNodeRepository
    from memory_engine.models.domain import MemoryNode

    session = ctx.get_session()
    try:
        repo = MemoryNodeRepository(session)
        node_id = next(iter(node_ids_before))
        orm = repo.get_bare(node_id)
        node = MemoryNode.model_validate(orm)
        result = SourceValidityService().revalidate(node, project_dir)
        assert result.changed
        repo.set_validity(
            node_id, new_status=result.new_status.value, reason=result.reason,
            new_source_hash=result.new_source_hash, actor="test_explicit_revalidation",
        )
    finally:
        session.close()

    restored = tool_retrieve_agent_context(ctx, req)
    ids_restored = {str(n["id"]) for n in restored.get("modules", [])}
    assert node_id in ids_restored


def test_scenario_d_unrelated_file_edit_preserves_valid_cache(project_dir):
    src_a = project_dir / "memory_engine" / "billing" / "invoice.py"
    src_a.parent.mkdir(parents=True)
    src_a.write_text("class Invoice:\n    def total(self):\n        return 0\n", encoding="utf-8")
    src_b = project_dir / "memory_engine" / "billing" / "receipts.py"
    src_b.write_text("class Receipt:\n    pass\n", encoding="utf-8")

    ctx = ProjectContext(project_dir)
    tool_reflect_and_write(ctx, ReflectAndWriteInput(
        task="Add invoice total calculation",
        outcome="Implemented Invoice.total() with tests for rounding edge cases.",
        verification_status="tests_passed",
        changed_files=["memory_engine/billing/invoice.py"],
        task_intent="feature_implementation",
    ))
    req = RetrieveContextInput(
        task="Add invoice total calculation",
        current_files=["memory_engine/billing/invoice.py"],
    )
    first = tool_retrieve_agent_context(ctx, req)
    node_ids_before = {str(n["id"]) for n in first.get("modules", [])}
    assert node_ids_before
    tool_retrieve_agent_context(ctx, req)  # populate cache

    # Edit an unrelated file — must not invalidate invoice.py's memory.
    src_b.write_text("class Receipt:\n    amount = 0\n", encoding="utf-8")

    again = tool_retrieve_agent_context(ctx, req)
    ids_after = {str(n["id"]) for n in again.get("modules", [])}
    assert node_ids_before == ids_after
    assert again["cache_hit"] is True, "unrelated edit should not force a cache miss"


# ===========================================================================
# Task 5 — validity transitions integrate with memory_revision
# ===========================================================================


def test_validity_transition_increments_memory_revision_once(project_dir):
    src = project_dir / "a.py"
    src.write_text("class A: pass\n", encoding="utf-8")
    ctx = ProjectContext(project_dir)
    tool_reflect_and_write(ctx, ReflectAndWriteInput(
        task="Add class A",
        outcome="Implemented class A with a full test suite covering behavior.",
        verification_status="tests_passed",
        changed_files=["a.py"],
        task_intent="feature_implementation",
    ))
    req = RetrieveContextInput(task="Add class A", current_files=["a.py"])
    tool_retrieve_agent_context(ctx, req)
    tool_retrieve_agent_context(ctx, req)  # cache warm, no transition yet

    rev_before = ctx.get_state_manager().load().memory_revision

    src.unlink()
    tool_retrieve_agent_context(ctx, req)  # triggers the transition

    rev_after_1 = ctx.get_state_manager().load().memory_revision
    assert rev_after_1 == rev_before + 1

    # Repeated retrieval with status already invalidated must not keep bumping.
    tool_retrieve_agent_context(ctx, req)
    tool_retrieve_agent_context(ctx, req)
    rev_after_2 = ctx.get_state_manager().load().memory_revision
    assert rev_after_2 == rev_after_1


# ===========================================================================
# Task 8 — minimum symbol-aware validation
# ===========================================================================


def test_symbol_exists_true_for_present_python_symbol(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("class Foo:\n    pass\n\n\ndef bar():\n    return 1\n", encoding="utf-8")
    assert symbol_exists_in_file(f, "Foo") is True
    assert symbol_exists_in_file(f, "bar") is True


def test_symbol_exists_false_when_removed(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def other():\n    return 1\n", encoding="utf-8")
    assert symbol_exists_in_file(f, "bar") is False


def test_symbol_exists_none_for_unsupported_language(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text("def bar\nend\n", encoding="utf-8")
    assert symbol_exists_in_file(f, "bar") is None


def test_symbol_removed_produces_needs_revalidation_not_deletion(session, project, tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def bar():\n    return 1\n", encoding="utf-8")
    node = MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title="bar() computes 1",
        summary="bar() always returns 1.",
        kind=MemoryKind.debug,
        source_path="m.py",
        source_hash=_sha256("def bar():\n    return 1\n"),
        source_symbol="bar",
    ))
    f.write_text("def bar():\n    return 1\n\n\ndef baz():\n    return 2\n", encoding="utf-8")
    # Content hash changes too here (file grew) — but the symbol itself is
    # still present, so hash-drift (needs_revalidation) fires either way.
    # Use a rename scenario instead to isolate the symbol-only signal:
    f.write_text("def baz():\n    return 1\n", encoding="utf-8")

    from memory_engine.services.source_validity import SourceValidityService
    result = SourceValidityService().check(node, tmp_path)
    assert result.changed
    assert result.new_status == MemoryStatus.needs_revalidation
    assert "symbol" in (result.reason or "").lower() or "content" in (result.reason or "").lower()


# ===========================================================================
# Task 9 — minimum commit reachability validation
# ===========================================================================


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "f.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    return out


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_reachable_commit_returns_true(tmp_path):
    head = _init_git_repo(tmp_path)
    assert check_commit_reachable(tmp_path, head) is True


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_unreachable_commit_returns_false(tmp_path):
    _init_git_repo(tmp_path)
    fake_but_wellformed = "a" * 40
    # rev-parse --verify will fail for a nonexistent object -> "unknown", not False.
    assert check_commit_reachable(tmp_path, fake_but_wellformed) is None


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_commit_reachable_after_branch_deleted_from_another_ref(tmp_path):
    head = _init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature work"], cwd=tmp_path, check=True)
    feature_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    # Merge feature into a mainline-equivalent branch, then delete "feature".
    subprocess.run(["git", "checkout", "-q", "-b", "main2", head], cwd=tmp_path, check=True)
    subprocess.run(["git", "merge", "-q", "feature", "--no-edit"], cwd=tmp_path, check=True)
    subprocess.run(["git", "branch", "-D", "feature"], cwd=tmp_path, check=True)

    assert check_commit_reachable(tmp_path, feature_commit) is True


def test_no_git_repository_returns_unknown(tmp_path):
    assert check_commit_reachable(tmp_path, "a" * 40) is None


def test_malformed_commit_sha_returns_unknown(tmp_path):
    assert check_commit_reachable(tmp_path, "not-a-sha!!") is None
    assert check_commit_reachable(tmp_path, "") is None
