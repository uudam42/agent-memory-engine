"""Tests for ProjectSeedingService (Phase 12)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from memory_engine.db.init_db import create_fts_tables
from memory_engine.models.domain import MemoryKind
from memory_engine.models.orm import Base, ProjectORM
from memory_engine.skills.seeding import ProjectSeedingService, SeedInput


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path}/test.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(db)
    with db.connect() as conn:
        create_fts_tables(conn)
        conn.commit()
    return db


@pytest.fixture
def session(engine):
    s = Session(engine)
    yield s
    s.close()


@pytest.fixture
def project(session):
    p = ProjectORM(name="test-project", description="test")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_seed_with_full_input(session, project, project_root):
    svc = ProjectSeedingService(session)
    result = svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        description="A task scheduling system with retry and lifecycle management.",
        constraints=["Terminal states (COMPLETED, FAILED) must never be exited."],
        decisions=["SQLite chosen over PostgreSQL for zero-infrastructure deployment."],
        tech_stack=["Python", "FastAPI", "SQLite"],
        conventions=["All PRs require one reviewer before merge."],
        skip_auto_extract=True,
    ))
    assert result.nodes_created == 4
    assert result.module_nodes == 1
    assert result.constraint_nodes == 1
    assert result.decision_nodes == 1
    assert result.procedure_nodes == 1
    assert len(result.node_titles) == 4
    assert result.skipped_reason is None


def test_seed_creates_correct_kinds(session, project, project_root):
    from memory_engine.models.orm import MemoryNodeORM
    svc = ProjectSeedingService(session)
    svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        description="A billing service.",
        constraints=["Never charge a customer twice for the same invoice."],
        decisions=["Event sourcing for audit trail."],
        skip_auto_extract=True,
    ))
    nodes = session.query(MemoryNodeORM).filter_by(project_id=str(project.id)).all()
    kinds = {n.kind for n in nodes}
    assert "module" in kinds
    assert "constraint" in kinds
    assert "decision" in kinds


def test_seed_nodes_are_active_with_full_confidence(session, project, project_root):
    from memory_engine.models.orm import MemoryNodeORM
    svc = ProjectSeedingService(session)
    svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        description="A monitoring pipeline.",
        constraints=["Alerts must fire within 30 seconds of threshold breach."],
        skip_auto_extract=True,
    ))
    nodes = session.query(MemoryNodeORM).filter_by(project_id=str(project.id)).all()
    for node in nodes:
        assert node.status == "active"
        assert node.confidence == 1.0


def test_seed_returns_skip_reason_when_no_content(session, project, project_root):
    svc = ProjectSeedingService(session)
    result = svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        skip_auto_extract=True,
    ))
    assert result.nodes_created == 0
    assert result.skipped_reason is not None


def test_seed_auto_extracts_from_readme(session, project, tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "# My Project\n\nThis is a data ingestion pipeline for real-time analytics.\n\n"
        "## Constraints\n\n- Never drop events — all failures must be retried.\n"
        "- Do not store PII outside the EU data region.\n\n"
        "## Architecture Decisions\n\n- Kafka chosen for event streaming.\n",
        encoding="utf-8",
    )
    svc = ProjectSeedingService(session)
    result = svc.seed(SeedInput(
        project_id=project.id,
        project_root=tmp_path,
    ))
    assert result.nodes_created > 0
    assert result.module_nodes == 1
    assert result.constraint_nodes >= 1


def test_seed_description_only(session, project, project_root):
    svc = ProjectSeedingService(session)
    result = svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        description="A simple note-taking app.",
        skip_auto_extract=True,
    ))
    assert result.nodes_created == 1
    assert result.module_nodes == 1
    assert result.constraint_nodes == 0


def test_seed_tech_stack_appended_to_description(session, project, project_root):
    from memory_engine.models.orm import MemoryNodeORM
    svc = ProjectSeedingService(session)
    svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        description="A REST API.",
        tech_stack=["Python", "FastAPI"],
        skip_auto_extract=True,
    ))
    node = session.query(MemoryNodeORM).filter_by(
        project_id=str(project.id), kind="module"
    ).first()
    assert node is not None
    assert "Python" in node.summary or "FastAPI" in node.summary


def test_seed_constraint_importance_is_high(session, project, project_root):
    from memory_engine.models.orm import MemoryNodeORM
    svc = ProjectSeedingService(session)
    svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        constraints=["Never expose API keys in logs."],
        skip_auto_extract=True,
    ))
    node = session.query(MemoryNodeORM).filter_by(
        project_id=str(project.id), kind="constraint"
    ).first()
    assert node is not None
    assert node.importance >= 0.88


def test_seed_multiple_constraints(session, project, project_root):
    svc = ProjectSeedingService(session)
    result = svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        constraints=[
            "No direct database access from HTTP handlers.",
            "All user inputs must be validated at the boundary.",
            "Rate limit: max 100 requests per second per IP.",
        ],
        skip_auto_extract=True,
    ))
    assert result.constraint_nodes == 3


def test_seed_conventions_bundled_into_one_procedure(session, project, project_root):
    svc = ProjectSeedingService(session)
    result = svc.seed(SeedInput(
        project_id=project.id,
        project_root=project_root,
        conventions=["Use conventional commits.", "Squash before merging.", "No --force push to main."],
        skip_auto_extract=True,
    ))
    assert result.procedure_nodes == 1


def test_tool_seed_project_context_integration(tmp_path):
    """Integration test: MCP tool wires through to seeding service."""
    from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService
    from memory_engine.mcp.project_context import get_project_context
    from memory_engine.mcp.schemas import SeedProjectInput
    from memory_engine.mcp.tools import tool_seed_project_context

    (tmp_path / "README.md").write_text("# Demo Project\nA demo.", encoding="utf-8")
    svc = ProjectBootstrapService(tmp_path)
    svc.bootstrap()

    ctx = get_project_context(tmp_path)
    ctx.ensure_bootstrapped()

    inp = SeedProjectInput(
        description="A demo project for integration testing.",
        constraints=["Do not write to disk outside the tmp directory."],
        skip_auto_extract=True,
    )
    result = tool_seed_project_context(ctx, inp)
    assert result["nodes_created"] == 2
    assert result["constraint_nodes"] == 1
    assert result["skipped_reason"] is None
