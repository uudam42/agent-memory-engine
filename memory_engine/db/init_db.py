"""Create all tables — safe to call multiple times (create_all is idempotent).

Phase 6 addition:
  create_fts_tables() creates the FTS5 virtual table for knowledge chunks.
  It is called by init_db() after SQLAlchemy creates the regular tables.
"""

from sqlalchemy import text

from memory_engine.db.session import engine
from memory_engine.models.orm import Base  # noqa: F401 — registers all mapped classes
import memory_engine.models.knowledge_orm  # noqa: F401 — registers Phase 6 ORM classes


def create_fts_tables(conn) -> None:  # type: ignore[type-arg]
    """Create FTS5 virtual table for knowledge chunk lexical search.

    Uses IF NOT EXISTS — safe to call multiple times.
    FTS5 is built into Python's sqlite3 by default.
    """
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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        create_fts_tables(conn)
        conn.commit()


if __name__ == "__main__":
    init_db()
    print("Database initialised.")
