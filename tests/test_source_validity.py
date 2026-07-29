"""Regression tests for Issue 1 — automatic stale-memory detection (Phase 15).

Uses real temporary files on disk (via tmp_path) and the real in-memory
SQLite session fixture. Exercises SourceValidityService directly and via
RecallService end-to-end, matching the required test list in the phase spec.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from memory_engine.models.domain import (
    MemoryKind,
    MemoryNodeCreate,
    MemoryStatus,
    ProjectCreate,
    RecallRequest,
)
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.services.source_validity import SourceValidityService, hash_file
from memory_engine.skills.recall import RecallService


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def project(session):
    return ProjectService(session).create(
        ProjectCreate(name="stale-detect-project", description="Issue 1 fixture")
    )


def _make_node(session, project, tmp_path: Path, *, kind=MemoryKind.architecture,
                rel_path="src/auth/service.py", content="class AuthService: pass\n",
                write_file=True, status=MemoryStatus.active):
    if write_file:
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        source_hash = _sha256(content)
    else:
        source_hash = _sha256(content)

    svc = MemoryService(session)
    node = svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="AuthService uses JWT",
        summary="AuthService issues and validates JWT access tokens.",
        kind=kind,
        status=status,
        source_path=rel_path,
        source_hash=source_hash,
    ))
    return node


# ---------------------------------------------------------------------------
# SourceValidityService unit tests
# ---------------------------------------------------------------------------


def test_source_file_deletion_invalidates(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    (tmp_path / node.source_path).unlink()

    result = SourceValidityService().check(node, tmp_path)

    assert result.changed is True
    assert result.new_status == MemoryStatus.invalidated
    assert "no longer exists" in result.reason


def test_source_file_content_change_needs_revalidation(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    (tmp_path / node.source_path).write_text("class AuthService: pass  # now uses OAuth2\n")

    result = SourceValidityService().check(node, tmp_path)

    assert result.changed is True
    assert result.new_status == MemoryStatus.needs_revalidation
    assert "content changed" in result.reason


def test_source_file_move_no_resolved_replacement(session, project, tmp_path):
    node = _make_node(session, project, tmp_path, rel_path="src/auth/service.py")
    (tmp_path / node.source_path).rename(tmp_path / "src/auth/service_v2.py")

    # No rename-tracking mechanism exists yet — the recorded path no longer
    # resolves, which must be treated exactly like deletion (conservative).
    result = SourceValidityService().check(node, tmp_path)

    assert result.changed is True
    assert result.new_status == MemoryStatus.invalidated


def test_unrelated_file_edit_does_not_invalidate(session, project, tmp_path):
    node = _make_node(session, project, tmp_path, rel_path="src/auth/service.py")
    other = tmp_path / "src/unrelated.py"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("print('hello')\n")

    result = SourceValidityService().check(node, tmp_path)

    assert result.changed is False


def test_legacy_memory_without_source_metadata_is_untouched(session, project, tmp_path):
    svc = MemoryService(session)
    node = svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Old memory",
        summary="Written before Phase 15 — no source_path recorded.",
        kind=MemoryKind.architecture,
    ))
    assert node.source_path is None

    result = SourceValidityService().check(node, tmp_path)

    assert result.changed is False


def test_no_source_hash_recorded_is_conservative(session, project, tmp_path):
    """A node with source_path but no source_hash (older writer) must not be
    marked stale just because the file's *content* changed — we have nothing
    to compare against. Existence is still checked."""
    f = tmp_path / "src/mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("def f(): pass\n")

    svc = MemoryService(session)
    node = svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Module note",
        summary="Some module note without a recorded hash.",
        kind=MemoryKind.module,
        source_path="src/mod.py",
    ))
    assert node.source_hash is None

    f.write_text("def f(): return 42\n")
    result = SourceValidityService().check(node, tmp_path)
    assert result.changed is False


def test_superseded_node_is_not_touched_by_validity_check(session, project, tmp_path):
    node = _make_node(session, project, tmp_path, status=MemoryStatus.superseded)
    (tmp_path / node.source_path).unlink()

    result = SourceValidityService().check(node, tmp_path)

    assert result.changed is False


def test_protected_architecture_memory_stale_source_is_invalidated_not_deleted(
    session, project, tmp_path
):
    """Architecture/decision/constraint memories are protected from deletion,
    but that must not mean they stay authoritative once their source is gone."""
    node = _make_node(session, project, tmp_path, kind=MemoryKind.architecture)
    (tmp_path / node.source_path).unlink()

    result = SourceValidityService().check(node, tmp_path)
    assert result.new_status == MemoryStatus.invalidated

    # The record itself must still exist (audit trail) — not deleted.
    from memory_engine.services.memory_service import MemoryService as MS
    still_there = MS(session).get_node(str(node.id))
    assert still_there is not None
    assert still_there.title == node.title


def test_revalidation_restores_eligibility(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    path = tmp_path / node.source_path
    path.unlink()

    check = SourceValidityService().check(node, tmp_path)
    assert check.new_status == MemoryStatus.invalidated

    # Restore the file with the ORIGINAL content.
    path.write_text("class AuthService: pass\n")

    from memory_engine.models.domain import MemoryNode as MN
    node.status = MemoryStatus.invalidated
    revalidated = SourceValidityService().revalidate(node, tmp_path)

    assert revalidated.changed is True
    assert revalidated.new_status == MemoryStatus.active
    assert revalidated.new_source_hash == hash_file(path)


def test_revalidation_audit_fields_recorded_via_repository(session, project, tmp_path):
    from memory_engine.repositories.memory_node import MemoryNodeRepository

    node = _make_node(session, project, tmp_path)
    repo = MemoryNodeRepository(session)

    updated = repo.set_validity(
        str(node.id),
        new_status=MemoryStatus.invalidated.value,
        reason="source file no longer exists: src/auth/service.py",
    )

    assert updated.status == "invalidated"
    assert updated.previous_status == "active"
    assert updated.validity_checked_at is not None
    assert "no longer exists" in updated.validity_reason


# ---------------------------------------------------------------------------
# End-to-end RecallService integration (Scenario A from the phase spec)
# ---------------------------------------------------------------------------


def test_recall_without_project_root_is_unaffected(session, project, tmp_path):
    """Backward compatibility: RecallService with no project_root (the default
    used by every pre-Phase-15 caller) must never touch the filesystem or
    change any node's status, even when a source_path is recorded and the
    file has since been deleted."""
    node = _make_node(session, project, tmp_path)
    (tmp_path / node.source_path).unlink()

    svc = RecallService(session)  # no project_root supplied
    result = svc.recall(RecallRequest(
        project_id=project.id,
        current_task="Explain how AuthService issues JWT tokens",
    ))

    reloaded = MemoryService(session).get_node(str(node.id))
    assert reloaded.status == MemoryStatus.active


def test_recall_excludes_invalidated_source_backed_memory(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    (tmp_path / node.source_path).unlink()

    svc = RecallService(session, project_root=tmp_path)
    result = svc.recall(RecallRequest(
        project_id=project.id,
        current_task="Explain how AuthService issues JWT tokens",
        current_files=["src/auth/service.py"],
    ))

    titles = [n.title for n in result.context_pack.architecture]
    assert node.title not in titles

    reloaded = MemoryService(session).get_node(str(node.id))
    assert reloaded.status == MemoryStatus.invalidated
    assert reloaded.previous_status == "active"

    exclusion_reasons = [
        te.reason for te in result.retrieval_trace
        if te.memory_id == str(node.id) and te.action == "excluded"
    ]
    assert any("invalidated" in r for r in exclusion_reasons)


def test_recall_reactivates_after_source_restored(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    path = tmp_path / node.source_path
    path.unlink()

    svc = RecallService(session, project_root=tmp_path)
    svc.recall(RecallRequest(
        project_id=project.id,
        current_task="Explain how AuthService issues JWT tokens",
    ))
    assert MemoryService(session).get_node(str(node.id)).status == MemoryStatus.invalidated

    # Restore and explicitly revalidate (recall alone must NOT silently
    # resurrect a node — that would defeat the audit requirement).
    path.write_text("class AuthService: pass\n")
    reloaded = MemoryService(session).get_node(str(node.id))
    check = SourceValidityService().revalidate(reloaded, tmp_path)
    assert check.changed is True

    from memory_engine.repositories.memory_node import MemoryNodeRepository
    MemoryNodeRepository(session).set_validity(
        str(node.id),
        new_status=check.new_status.value,
        reason=check.reason,
        new_source_hash=check.new_source_hash,
        actor="manual_revalidation",
    )

    result = svc.recall(RecallRequest(
        project_id=project.id,
        current_task="Explain how AuthService issues JWT tokens",
    ))
    titles = [n.title for n in result.context_pack.architecture]
    assert node.title in titles


def test_recall_does_not_invalidate_on_unrelated_edit(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    other = tmp_path / "src/unrelated.py"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("print('unrelated change')\n")

    svc = RecallService(session, project_root=tmp_path)
    svc.recall(RecallRequest(
        project_id=project.id,
        current_task="Explain how AuthService issues JWT tokens",
    ))

    reloaded = MemoryService(session).get_node(str(node.id))
    assert reloaded.status == MemoryStatus.active


def test_needs_revalidation_excluded_from_active_context(session, project, tmp_path):
    node = _make_node(session, project, tmp_path)
    (tmp_path / node.source_path).write_text("class AuthService: pass  # OAuth2 now\n")

    svc = RecallService(session, project_root=tmp_path)
    result = svc.recall(RecallRequest(
        project_id=project.id,
        current_task="Explain how AuthService issues JWT tokens",
    ))

    titles = [n.title for n in result.context_pack.architecture]
    assert node.title not in titles
    reloaded = MemoryService(session).get_node(str(node.id))
    assert reloaded.status == MemoryStatus.needs_revalidation


# ---------------------------------------------------------------------------
# Legacy database migration test — real sqlite file, pre-Phase-15 schema
# ---------------------------------------------------------------------------


def test_legacy_sqlite_database_migrates_and_old_rows_stay_readable(tmp_path):
    """Simulates opening a real .db file created before Phase 15 (no
    source_path/source_hash/validity_* columns on memory_nodes) and verifies
    apply_schema_migrations() adds the new columns additively without
    touching existing data, and old rows remain fully readable afterward."""
    import sqlite3
    import uuid as uuid_mod

    from sqlalchemy import create_engine, text as sa_text
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "legacy.db"

    # Build a pre-Phase-15 schema by hand (subset sufficient for this test):
    # a projects table and a memory_nodes table WITHOUT the new columns.
    raw = sqlite3.connect(str(db_path))
    raw.executescript(
        """
        CREATE TABLE projects (
            id VARCHAR(36) PRIMARY KEY,
            name VARCHAR(128) NOT NULL UNIQUE,
            description TEXT,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE memory_nodes (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            parent_id VARCHAR(36),
            title VARCHAR(256) NOT NULL,
            summary TEXT NOT NULL,
            kind VARCHAR(32) NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            tags TEXT,
            created_at DATETIME,
            updated_at DATETIME,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            confidence FLOAT NOT NULL DEFAULT 1.0,
            importance FLOAT NOT NULL DEFAULT 0.5,
            module_path VARCHAR(512)
        );
        CREATE TABLE evidence (
            id VARCHAR(36) PRIMARY KEY,
            memory_node_id VARCHAR(36) NOT NULL,
            content TEXT NOT NULL,
            source VARCHAR(512),
            created_at DATETIME
        );
        """
    )
    project_id = str(uuid_mod.uuid4())
    node_id = str(uuid_mod.uuid4())
    raw.execute(
        "INSERT INTO projects (id, name, description, created_at, updated_at) "
        "VALUES (?, 'legacy-project', 'pre-phase-15', datetime('now'), datetime('now'))",
        (project_id,),
    )
    raw.execute(
        "INSERT INTO memory_nodes (id, project_id, title, summary, kind, depth, tags, "
        "created_at, updated_at, status, confidence, importance) "
        "VALUES (?, ?, 'Legacy title', 'Legacy summary', 'architecture', 0, '[]', "
        "datetime('now'), datetime('now'), 'active', 1.0, 0.5)",
        (node_id, project_id),
    )
    raw.commit()
    raw.close()

    # Sanity: the new columns do not exist yet.
    conn = sqlite3.connect(str(db_path))
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")}
    conn.close()
    assert "source_path" not in cols_before
    assert "source_hash" not in cols_before

    # Run the real migration path against this file.
    from memory_engine.db.init_db import apply_schema_migrations

    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        apply_schema_migrations(conn)

    conn = sqlite3.connect(str(db_path))
    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")}
    conn.close()
    for expected in (
        "source_path", "source_hash", "validity_reason",
        "validity_checked_at", "previous_status",
    ):
        assert expected in cols_after

    # Old row remains readable through the ORM after migration, and new
    # columns default to NULL (never guessed as global/authoritative).
    Session = sessionmaker(bind=eng)
    from memory_engine.models.orm import MemoryNodeORM

    s = Session()
    try:
        node = s.get(MemoryNodeORM, node_id)
        assert node is not None
        assert node.title == "Legacy title"
        assert node.source_path is None
        assert node.source_hash is None
        assert node.status == "active"

        # SourceValidityService must be a complete no-op on this legacy row.
        from memory_engine.models.domain import MemoryNode
        from memory_engine.services.source_validity import SourceValidityService

        domain_node = MemoryNode.model_validate(node)
        result = SourceValidityService().check(domain_node, tmp_path)
        assert result.changed is False
    finally:
        s.close()
        eng.dispose()


# ---------------------------------------------------------------------------
# Phase 3A review (A8) — bounded I/O for source-validity checks in RecallService
# ---------------------------------------------------------------------------


def test_recall_bounds_source_validity_checks_per_call(session, project, tmp_path):
    """A cold recall() call must not hash every source-backed memory in the
    project unconditionally — that scales with total project memory count,
    not with the token budget or the number of memories actually selected,
    and is exactly the unbounded-I/O pattern the phase spec (Review A8)
    prohibits. Create far more source-backed nodes than any reasonable
    per-request cap and assert the number of underlying hash_file() calls
    stays bounded regardless of how many qualify.
    """
    n_nodes = 200
    for i in range(n_nodes):
        rel_path = f"src/mod_{i}.py"
        f = tmp_path / rel_path
        f.parent.mkdir(parents=True, exist_ok=True)
        content = f"class Mod{i}: pass\n"
        f.write_text(content)
        MemoryService(session).create_node(MemoryNodeCreate(
            project_id=project.id,
            title=f"Mod{i} note",
            summary=f"Module {i} does something.",
            kind=MemoryKind.module,
            status=MemoryStatus.active,
            source_path=rel_path,
            source_hash=_sha256(content),
        ))

    svc = RecallService(session, project_root=tmp_path)

    with patch(
        "memory_engine.services.source_validity.hash_file", wraps=hash_file
    ) as spy:
        svc.recall(RecallRequest(
            project_id=project.id,
            current_task="general project status",
            token_budget=6000,
        ))
        call_count = spy.call_count

    assert call_count < n_nodes, (
        f"recall() performed {call_count} hash_file() reads for {n_nodes} "
        "source-backed memories — validity checking is not bounded per "
        "request (Phase 3A Review A8)."
    )
