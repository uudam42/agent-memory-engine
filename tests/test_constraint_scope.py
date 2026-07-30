"""Tests for Issue 2 — scope-aware memory constraints (Phase 16).

Before this change, every ``constraint`` memory bypassed the topical
relevance gate unconditionally, so an unrelated constraint (e.g. a
PaymentService integer-cents rule) would surface on completely unrelated
tasks (e.g. a renderer bug fix). These tests exercise the new
ConstraintScope model end-to-end through RecallService, plus the
conservative legacy-migration rule, serialization, and cache invalidation.
"""

from __future__ import annotations

import pytest

from memory_engine.models.domain import (
    ConstraintScope,
    MemoryKind,
    MemoryNodeCreate,
    MemoryStatus,
    ProjectCreate,
    RecallRequest,
    SourceTrust,
)
from memory_engine.services.constraint_scope import (
    constraint_is_eligible,
    effective_scope,
    infer_candidate_scope,
    infer_legacy_scope,
)
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.skills.recall import RecallService


@pytest.fixture()
def project(session):
    return ProjectService(session).create(
        ProjectCreate(name="scope-project", description="Issue 2 fixture")
    )


def _make_constraint(session, project, *, title, summary, tags=None, module_path=None,
                      constraint_scope=None, constraint_scope_ref=None, branch_name=None,
                      branch_scope=None, confidence=0.95, status=MemoryStatus.active,
                      trust_level=SourceTrust.reviewed_committed_design.value):
    # Issue 3: these fixtures predate the source trust model and exercise
    # scope eligibility, not trust — default to a trust level that clears
    # the authority threshold so existing scope-only assertions are
    # unaffected. Tests that specifically need to prove trust (not just
    # confidence) gates global authority pass trust_level=None/low explicitly
    # (see test_source_trust.py).
    return MemoryService(session).create_node(MemoryNodeCreate(
        project_id=project.id,
        title=title,
        summary=summary,
        kind=MemoryKind.constraint,
        tags=tags or [],
        module_path=module_path,
        constraint_scope=constraint_scope,
        status=status,
        confidence=confidence,
        trust_level=trust_level,
    ))


def _recall(session, project, task, **kwargs):
    svc = RecallService(session)
    req = RecallRequest(project_id=project.id, current_task=task, token_budget=6000, **kwargs)
    return svc.recall(req)


def _constraint_titles(result):
    return {n.title for n in result.context_pack.constraints}


# ---------------------------------------------------------------------------
# Global constraints
# ---------------------------------------------------------------------------


def test_trusted_global_constraint_bypasses_relevance_gate(session, project):
    _make_constraint(
        session, project,
        title="Never log secrets",
        summary="Secrets must never be written to logs.",
        constraint_scope=ConstraintScope.global_.value,
        confidence=0.95,
    )
    result = _recall(session, project, "Implement the renderer alpha blending fix")
    assert "Never log secrets" in _constraint_titles(result)


def test_untrusted_global_constraint_does_not_bypass_gate(session, project):
    _make_constraint(
        session, project,
        title="Low confidence global rule",
        summary="A global-scoped rule with low confidence.",
        constraint_scope=ConstraintScope.global_.value,
        confidence=0.5,  # below _MIN_GLOBAL_CONFIDENCE
    )
    result = _recall(session, project, "Implement the renderer alpha blending fix")
    assert "Low confidence global rule" not in _constraint_titles(result)


def test_global_constraint_respects_branch_restriction(session, project):
    node = _make_constraint(
        session, project,
        title="Feature-branch-only global rule",
        summary="Applies only on the feature branch.",
        constraint_scope=ConstraintScope.global_.value,
        confidence=0.95,
    )
    # Directly patch branch_name via repository since MemoryNodeCreate has no branch_name field.
    from memory_engine.repositories.memory_node import MemoryNodeRepository
    MemoryNodeRepository(session).update_fields(str(node.id), branch_name="feature/x")

    result = _recall(session, project, "unrelated task", current_branch="main")
    assert "Feature-branch-only global rule" not in _constraint_titles(result)

    result2 = _recall(session, project, "unrelated task", current_branch="feature/x")
    assert "Feature-branch-only global rule" in _constraint_titles(result2)


# ---------------------------------------------------------------------------
# Repository scope
# ---------------------------------------------------------------------------


def test_repository_constraint_always_applies_within_project(session, project):
    _make_constraint(
        session, project,
        title="Repository-wide style rule",
        summary="All modules in this repository follow snake_case naming.",
        constraint_scope=ConstraintScope.repository.value,
    )
    result = _recall(session, project, "Completely unrelated renderer topic")
    assert "Repository-wide style rule" in _constraint_titles(result)


# ---------------------------------------------------------------------------
# Module / path / symbol scope
# ---------------------------------------------------------------------------


def test_module_constraint_requires_structural_overlap(session, project):
    _make_constraint(
        session, project,
        title="PaymentService must use integer cents",
        summary="PaymentService must use integer cents, never floats, for money.",
        module_path="payment_service",
        constraint_scope=ConstraintScope.module.value,
    )
    renderer_result = _recall(
        session, project, "Fix renderer alpha blending bug",
        current_files=["renderer/blend.py"],
    )
    assert "PaymentService must use integer cents" not in _constraint_titles(renderer_result)

    payment_result = _recall(
        session, project, "Update payment_service billing logic",
        current_files=["payment_service/billing.py"],
    )
    assert "PaymentService must use integer cents" in _constraint_titles(payment_result)


def test_path_constraint_requires_exact_current_file(session, project):
    from memory_engine.repositories.memory_node import MemoryNodeRepository
    node = _make_constraint(
        session, project,
        title="auth.py specific rule",
        summary="This file has a special locking requirement.",
        constraint_scope=ConstraintScope.path.value,
    )
    MemoryNodeRepository(session).update_fields(
        str(node.id), constraint_scope_ref="src/auth/service.py"
    )

    unrelated = _recall(session, project, "generic task", current_files=["src/other.py"])
    assert "auth.py specific rule" not in _constraint_titles(unrelated)

    matching = _recall(session, project, "generic task", current_files=["src/auth/service.py"])
    assert "auth.py specific rule" in _constraint_titles(matching)


def test_symbol_constraint_requires_symbol_overlap(session, project):
    _make_constraint(
        session, project,
        title="AuthService symbol rule",
        summary="AuthService.validate must always check expiry.",
        tags=["AuthService"],
        constraint_scope=ConstraintScope.symbol.value,
    )
    unrelated = _recall(session, project, "generic task", current_symbols=["OtherThing"])
    assert "AuthService symbol rule" not in _constraint_titles(unrelated)

    matching = _recall(session, project, "generic task", current_symbols=["AuthService"])
    assert "AuthService symbol rule" in _constraint_titles(matching)


# ---------------------------------------------------------------------------
# Branch scope
# ---------------------------------------------------------------------------


def test_branch_constraint_requires_branch_match(session, project):
    from memory_engine.repositories.memory_node import MemoryNodeRepository
    node = _make_constraint(
        session, project,
        title="Branch scoped rule",
        summary="Only applies on the release branch.",
        constraint_scope=ConstraintScope.branch.value,
    )
    MemoryNodeRepository(session).update_fields(str(node.id), branch_name="release/1.0")

    wrong_branch = _recall(session, project, "generic task", current_branch="main")
    assert "Branch scoped rule" not in _constraint_titles(wrong_branch)

    right_branch = _recall(session, project, "generic task", current_branch="release/1.0")
    assert "Branch scoped rule" in _constraint_titles(right_branch)


# ---------------------------------------------------------------------------
# Task-intent scope
# ---------------------------------------------------------------------------


def test_task_intent_constraint_eligibility_unit(session, project):
    node = _make_constraint(
        session, project,
        title="Bug-fix-only checklist rule 2",
        summary="During bug fixes, always add a regression test first.",
        tags=["intent:bug_fix"],
        constraint_scope=ConstraintScope.task_intent.value,
    )
    assert constraint_is_eligible(node, task_intent="bug_fix") is True
    assert constraint_is_eligible(node, task_intent="refactor") is False
    assert constraint_is_eligible(node, task_intent=None) is False


# ---------------------------------------------------------------------------
# Unrelated constraint exclusion (Scenario A from the phase spec)
# ---------------------------------------------------------------------------


def test_unrelated_constraint_excluded_scenario(session, project):
    _make_constraint(
        session, project,
        title="Global credential rule",
        summary="Credentials must always be stored via the secrets manager.",
        constraint_scope=ConstraintScope.global_.value,
        confidence=0.95,
    )
    _make_constraint(
        session, project,
        title="PaymentService integer cents",
        summary="PaymentService must use integer cents.",
        module_path="payment_service",
        constraint_scope=ConstraintScope.module.value,
    )
    _make_constraint(
        session, project,
        title="Renderer alpha blending",
        summary="Renderer must preserve alpha blending.",
        module_path="renderer",
        constraint_scope=ConstraintScope.module.value,
    )

    result = _recall(
        session, project, "Fix renderer blending artifact",
        current_files=["renderer/compositor.py"],
    )
    titles = _constraint_titles(result)
    assert "Global credential rule" in titles
    assert "Renderer alpha blending" in titles
    assert "PaymentService integer cents" not in titles


# ---------------------------------------------------------------------------
# Legacy unscoped constraints
# ---------------------------------------------------------------------------


def test_legacy_unscoped_constraint_infers_repository_not_global(session, project):
    node = _make_constraint(
        session, project,
        title="Legacy unscoped constraint",
        summary="Some old constraint with no explicit scope metadata.",
    )
    assert node.constraint_scope is None
    assert infer_legacy_scope(node) == ConstraintScope.repository
    assert effective_scope(node) == ConstraintScope.repository

    # Still surfaces (repository scope), but was never silently marked global.
    result = _recall(session, project, "Totally unrelated task about renderers")
    assert "Legacy unscoped constraint" in _constraint_titles(result)


def test_infer_candidate_scope_never_returns_global():
    for kwargs in [
        dict(module_path=None, touched_files=[], touched_symbols=[],
             branch_name=None, branch_explicit=False),
        dict(module_path="mod.x", touched_files=["a.py"], touched_symbols=["Foo"],
             branch_name=None, branch_explicit=False),
        dict(module_path=None, touched_files=["a.py"], touched_symbols=[],
             branch_name=None, branch_explicit=False),
        dict(module_path=None, touched_files=["a.py"], touched_symbols=["Foo"],
             branch_name=None, branch_explicit=False),
    ]:
        assert infer_candidate_scope(**kwargs) != ConstraintScope.global_


# ---------------------------------------------------------------------------
# Stale protected constraint
# ---------------------------------------------------------------------------


def test_stale_global_constraint_is_not_authoritative(session, project):
    _make_constraint(
        session, project,
        title="Stale global rule",
        summary="This used to be a global rule.",
        constraint_scope=ConstraintScope.global_.value,
        confidence=0.95,
        status=MemoryStatus.stale,
    )
    result = _recall(session, project, "Any unrelated task")
    # Excluded from the authoritative constraints bucket (composer filters
    # non-active statuses by default) even though scope eligibility alone
    # would have allowed it — audit trail is preserved on the node itself.
    assert "Stale global rule" not in _constraint_titles(result)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_legacy_database_migrates_constraint_scope_columns(tmp_path):
    import sqlite3

    from sqlalchemy import create_engine

    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "CREATE TABLE memory_nodes (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, "
        "summary TEXT, kind TEXT, depth INTEGER, tags TEXT, created_at TEXT, "
        "updated_at TEXT, status TEXT, confidence REAL, importance REAL)"
    )
    raw.execute(
        "CREATE TABLE memory_candidates (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, "
        "summary TEXT, proposed_kind TEXT, proposed_tags TEXT, created_at TEXT, "
        "updated_at TEXT, status TEXT, confidence REAL, importance REAL)"
    )
    raw.commit()
    raw.close()

    from memory_engine.db.init_db import apply_schema_migrations

    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        apply_schema_migrations(conn)

    conn = sqlite3.connect(str(db_path))
    node_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")}
    cand_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
    conn.close()
    eng.dispose()

    assert "constraint_scope" in node_cols
    assert "constraint_scope_ref" in node_cols
    assert "proposed_constraint_scope" in cand_cols
    assert "proposed_constraint_scope_ref" in cand_cols


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_constraint_scope_round_trips_through_orm(session, project):
    node = _make_constraint(
        session, project,
        title="Serialization check",
        summary="Round trips through the ORM correctly.",
        module_path="mod.a",
        constraint_scope=ConstraintScope.module.value,
    )
    from memory_engine.models.domain import MemoryNode
    from memory_engine.repositories.memory_node import MemoryNodeRepository

    orm = MemoryNodeRepository(session).get_bare(str(node.id))
    reloaded = MemoryNode.model_validate(orm)
    assert reloaded.constraint_scope == ConstraintScope.module.value


# ---------------------------------------------------------------------------
# Cache invalidation after scope change
# ---------------------------------------------------------------------------


def test_cache_invalidates_after_constraint_scope_change(session, project, tmp_path):
    from memory_engine.knowledge.cache import SimpleCache
    from memory_engine.knowledge.fusion import UnifiedContextRetrievalService
    from memory_engine.models.knowledge_domain import UnifiedRetrievalRequest
    from memory_engine.repositories.memory_node import MemoryNodeRepository

    node = _make_constraint(
        session, project,
        title="Cache scope-change rule",
        summary="Starts module-scoped, gets promoted to global.",
        module_path="unrelated_module",
        constraint_scope=ConstraintScope.module.value,
        confidence=0.95,
    )

    cache = SimpleCache()
    svc = UnifiedContextRetrievalService(session, cache=cache, project_root=str(tmp_path))
    req = UnifiedRetrievalRequest(
        project_id=project.id, task="Renderer bug fix", token_budget=6000,
        current_files=["renderer/x.py"], include_knowledge=False,
    )

    pack1 = svc.retrieve(req)
    assert not any(n.title == "Cache scope-change rule" for n in pack1.constraints)

    # Promote scope to global via the repository directly, then invalidate
    # the cache the same way PromotionService does on any memory write
    # (production code invalidates the process-wide cache; this test uses
    # its own local SimpleCache instance, so it invalidates that instance
    # directly to exercise the identical invalidate_project() contract).
    MemoryNodeRepository(session).update_fields(
        str(node.id), constraint_scope=ConstraintScope.global_.value
    )
    cache.invalidate_project(str(project.id))

    pack2 = svc.retrieve(req)
    assert any(n.title == "Cache scope-change rule" for n in pack2.constraints)
