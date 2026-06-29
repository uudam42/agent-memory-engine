"""Phase 13 — vector table migration tests."""

from __future__ import annotations

import sqlite3

import pytest

from memory_engine.db.init_db import migrate_vector_tables


def _table_exists(conn, name) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def test_fresh_db_creates_vec_metadata(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "vector.db"))
    report = migrate_vector_tables(conn, dimension=384)
    assert report["created"] is True
    assert _table_exists(conn, "vec_metadata")
    conn.close()


def test_migration_is_idempotent(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "vector.db"))
    migrate_vector_tables(conn, dimension=384)
    report2 = migrate_vector_tables(conn, dimension=384)
    # Second run on same dimension: nothing created, nothing cleared.
    assert report2["created"] is False
    assert report2["dimension_changed"] is False
    assert report2["cleared_count"] == 0
    conn.close()


def test_existing_lexical_only_db_migrates_without_loss(tmp_path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT, content TEXT)")
    conn.execute("INSERT INTO knowledge_chunks VALUES ('c1', 'hello')")
    conn.commit()

    migrate_vector_tables(conn, dimension=0)
    # Original lexical data preserved, vec_metadata added.
    assert _table_exists(conn, "vec_metadata")
    rows = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    assert rows == 1
    conn.close()


def test_dimension_change_clears_stale_vectors(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "vector.db"))
    migrate_vector_tables(conn, dimension=384)
    conn.execute(
        "INSERT INTO vec_metadata "
        "(record_id, project_id, content_hash, model, dimension) "
        "VALUES ('r1', 'p1', 'h', 'm', 384)"
    )
    conn.commit()

    report = migrate_vector_tables(conn, dimension=768)
    assert report["dimension_changed"] is True
    assert report["cleared_count"] == 1
    remaining = conn.execute("SELECT COUNT(*) FROM vec_metadata").fetchone()[0]
    assert remaining == 0
    conn.close()
