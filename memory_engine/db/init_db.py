"""Create all tables — safe to call multiple times (create_all is idempotent).

Phase 6 addition:
  create_fts_tables() creates the FTS5 virtual table for knowledge chunks.

Phase 9 addition:
  apply_schema_migrations() adds branch-aware columns to existing tables via
  ALTER TABLE ADD COLUMN. SQLite supports ADD COLUMN and ignores duplicates via
  the "duplicate column name" error which we catch and discard.

Phase 11 addition:
  Retention columns: memory_nodes.(archived_at, archived_reason,
  compacted_into_id, last_retrieved_at, retrieval_count) and
  memory_candidates.(expires_at, expiry_reason).
"""

from sqlalchemy import text

from memory_engine.db.session import engine
from memory_engine.models.orm import Base  # noqa: F401 — registers all mapped classes
import memory_engine.models.knowledge_orm  # noqa: F401 — registers Phase 6 + Phase 10 ORM classes


# Phase 9: branch-aware columns added to existing tables.
# Each tuple is (table, column, definition).
_BRANCH_COLUMNS: list[tuple[str, str, str]] = [
    # memory_nodes
    ("memory_nodes", "branch_name", "VARCHAR(128)"),
    ("memory_nodes", "commit_sha", "VARCHAR(64)"),
    ("memory_nodes", "branch_scope", "VARCHAR(32) DEFAULT 'global'"),
    ("memory_nodes", "source_revision", "VARCHAR(64)"),
    ("memory_nodes", "branch_promotion_eligible", "INTEGER DEFAULT 0"),
    # evidence
    ("evidence", "branch_name", "VARCHAR(128)"),
    ("evidence", "commit_sha", "VARCHAR(64)"),
    ("evidence", "branch_scope", "VARCHAR(32) DEFAULT 'global'"),
    # memory_candidates
    ("memory_candidates", "branch_name", "VARCHAR(128)"),
    ("memory_candidates", "commit_sha", "VARCHAR(64)"),
    ("memory_candidates", "branch_scope", "VARCHAR(32) DEFAULT 'current_branch'"),
    # knowledge_documents (branch_name already exists — skip; add extras)
    ("knowledge_documents", "commit_sha", "VARCHAR(64)"),
    ("knowledge_documents", "branch_scope", "VARCHAR(32) DEFAULT 'global'"),
    ("knowledge_documents", "source_revision", "VARCHAR(64)"),
    ("knowledge_documents", "valid_from_revision", "VARCHAR(64)"),
    ("knowledge_documents", "valid_to_revision", "VARCHAR(64)"),
    # knowledge_chunks
    ("knowledge_chunks", "branch_name", "VARCHAR(128)"),
    ("knowledge_chunks", "branch_scope", "VARCHAR(32) DEFAULT 'global'"),
    ("knowledge_chunks", "source_revision", "VARCHAR(64)"),
    ("knowledge_chunks", "commit_sha", "VARCHAR(64)"),
    # Phase 10: knowledge_documents — document/module/architecture summaries
    ("knowledge_documents", "document_summary", "TEXT"),
    ("knowledge_documents", "module_summary", "TEXT"),
    ("knowledge_documents", "architecture_summary", "TEXT"),
    ("knowledge_documents", "key_symbols", "TEXT DEFAULT '[]'"),
    ("knowledge_documents", "dependencies", "TEXT DEFAULT '[]'"),
    ("knowledge_documents", "related_documents", "TEXT DEFAULT '[]'"),
    # Phase 11: retention columns
    ("memory_nodes", "archived_at", "DATETIME"),
    ("memory_nodes", "archived_reason", "VARCHAR(512)"),
    ("memory_nodes", "compacted_into_id", "VARCHAR(36)"),
    ("memory_nodes", "last_retrieved_at", "DATETIME"),
    ("memory_nodes", "retrieval_count", "INTEGER DEFAULT 0"),
    ("memory_candidates", "expires_at", "DATETIME"),
    ("memory_candidates", "expiry_reason", "VARCHAR(512)"),
]


def apply_schema_migrations(conn) -> None:  # type: ignore[type-arg]
    """Add Phase 9 branch-aware columns where missing.

    Uses ADD COLUMN — safe on SQLite. Duplicate column errors are silently
    ignored so this function is fully idempotent.
    """
    for table, col, defn in _BRANCH_COLUMNS:
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {defn}"))
        except Exception:
            # Column already exists — ignore
            pass


def create_fts_tables(conn) -> None:  # type: ignore[type-arg]
    """Create FTS5 virtual tables for all knowledge granularities.

    Uses IF NOT EXISTS — safe to call multiple times.
    FTS5 is built into Python's sqlite3 by default.
    """
    # Phase 6: chunk-level FTS5
    conn.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
        USING fts5(
            chunk_id UNINDEXED,
            content,
            heading_text,
            symbols_text,
            module_text,
            tags_text,
            tokenize = 'porter unicode61'
        )
    """))
    # Phase 10: paragraph-level FTS5
    conn.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_paragraphs_fts
        USING fts5(
            paragraph_id UNINDEXED,
            content,
            summary,
            section_heading,
            symbols_text,
            tokenize = 'porter unicode61'
        )
    """))
    # Phase 10: proposition-level FTS5
    conn.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_propositions_fts
        USING fts5(
            proposition_id UNINDEXED,
            proposition_text,
            proposition_type UNINDEXED,
            tokenize = 'porter unicode61'
        )
    """))
    # Phase 10: chunk/module summary FTS5
    conn.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_summaries_fts
        USING fts5(
            summary_id UNINDEXED,
            summary_text,
            purpose,
            symbols_text,
            granularity_level UNINDEXED,
            tokenize = 'porter unicode61'
        )
    """))


def migrate_vector_tables(conn, dimension: int = 0) -> dict:  # type: ignore[type-arg]
    """Phase 13: safe migration for the persistent vector metadata table.

    Operates on a raw sqlite3 connection for the project-local vector.db.

    - Creates vec_metadata if missing (CREATE TABLE IF NOT EXISTS — safe).
    - Never alters or drops FTS5 tables (those live in memory.db).
    - If a non-zero dimension is supplied and differs from the stored dimension,
      logs a warning and clears stale vec records so a clean re-index can run.

    Returns a small report dict: {created, dimension_changed, cleared_count}.
    """
    report = {"created": False, "dimension_changed": False, "cleared_count": 0}

    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_metadata'"
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vec_metadata (
                rowid          INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id      TEXT NOT NULL UNIQUE,
                project_id     TEXT NOT NULL,
                record_type    TEXT NOT NULL DEFAULT 'chunk',
                branch_name    TEXT,
                branch_scope   TEXT,
                lifecycle_state TEXT NOT NULL DEFAULT 'active',
                content_hash   TEXT NOT NULL,
                model          TEXT NOT NULL,
                dimension      INTEGER NOT NULL DEFAULT 0,
                source_path    TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_vec_metadata_project "
            "ON vec_metadata(project_id, lifecycle_state)"
        )
        report["created"] = True
        conn.commit()
        return report

    if dimension and dimension > 0:
        row = conn.execute(
            "SELECT dimension, COUNT(*) FROM vec_metadata "
            "WHERE dimension > 0 GROUP BY dimension ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if row is not None and int(row[0]) != dimension:
            report["dimension_changed"] = True
            cleared = conn.execute("SELECT COUNT(*) FROM vec_metadata").fetchone()[0]
            conn.execute("DELETE FROM vec_metadata")
            try:
                conn.execute("DROP TABLE IF EXISTS vec_items")
            except Exception:
                pass
            report["cleared_count"] = int(cleared)
            conn.commit()
    return report


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        create_fts_tables(conn)
        apply_schema_migrations(conn)
        conn.commit()


if __name__ == "__main__":
    init_db()
    print("Database initialised.")
