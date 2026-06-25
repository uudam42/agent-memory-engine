"""FTS5 lexical index for KnowledgeChunks.

Uses SQLite's built-in FTS5 extension via raw SQL (SQLAlchemy does not have
a first-class FTS5 ORM mapping).

Virtual table schema:
  knowledge_chunks_fts (
    chunk_id,
    content,
    heading_text,   -- flattened heading_path for better title matching
    symbols_text,   -- flattened related_symbols
    module_text,    -- flattened module_paths
    tags_text
  )
  content='knowledge_chunks'    -- content table for triggers-free operation
  content_rowid='rowid'

The FTS5 table is created by init_db.create_fts_tables().

Index writes:
  fts_insert(conn, chunk_orm)
  fts_delete(conn, chunk_id)
  fts_rebuild(conn)

Query:
  fts_search(conn, project_id, query, limit) → list[FTSHit]
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class FTSHit:
    chunk_id: str
    rank: float   # FTS5 rank (negative; more negative = more relevant)
    snippet: str


_DDL_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
USING fts5(
    chunk_id UNINDEXED,
    content,
    heading_text,
    symbols_text,
    module_text,
    tags_text,
    tokenize = 'porter unicode61'
);
"""


def create_fts_table(conn) -> None:  # type: ignore[type-arg]
    """Create the FTS5 virtual table.  Safe to call multiple times (IF NOT EXISTS)."""
    conn.execute(text(_DDL_FTS5))


def fts_insert(session: Session, chunk_id: str, content: str,
               heading_path: list[str], symbols: list[str],
               module_paths: list[str], tags: list[str]) -> None:
    """Insert a chunk into the FTS5 table."""
    session.execute(text(
        "INSERT INTO knowledge_chunks_fts"
        " (chunk_id, content, heading_text, symbols_text, module_text, tags_text)"
        " VALUES (:cid, :content, :heading, :symbols, :module, :tags)"
    ), {
        "cid": chunk_id,
        "content": content[:8192],       # FTS5 has no hard limit but keep sane
        "heading": " ".join(heading_path),
        "symbols": " ".join(symbols),
        "module": " ".join(module_paths),
        "tags": " ".join(tags),
    })


def fts_delete(session: Session, chunk_id: str) -> None:
    """Remove a chunk from the FTS5 index."""
    session.execute(text(
        "DELETE FROM knowledge_chunks_fts WHERE chunk_id = :cid"
    ), {"cid": chunk_id})


def fts_rebuild(session: Session) -> None:
    """Rebuild the FTS5 index from scratch (useful after bulk deletes)."""
    session.execute(text("INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts) VALUES('rebuild')"))


def fts_search(
    session: Session,
    project_id: str,
    query: str,
    limit: int = 20,
) -> list[FTSHit]:
    """Run an FTS5 MATCH query and return ranked hits.

    Joins back to knowledge_chunks for project_id filtering.
    Falls back gracefully if FTS5 table doesn't exist or query is invalid.
    """
    if not query.strip():
        return []

    # Sanitize query for FTS5 (no raw quotes / operators)
    safe_query = _fts_escape(query)
    if not safe_query:
        return []

    try:
        rows = session.execute(text("""
            SELECT f.chunk_id, f.rank,
                   snippet(knowledge_chunks_fts, 1, '<b>', '</b>', '...', 20) AS snippet
            FROM   knowledge_chunks_fts f
            JOIN   knowledge_chunks     c ON c.chunk_id = f.chunk_id
            WHERE  knowledge_chunks_fts MATCH :q
              AND  c.project_id = :pid
              AND  c.index_status = 'indexed'
            ORDER BY f.rank
            LIMIT  :lim
        """), {"q": safe_query, "pid": project_id, "lim": limit}).fetchall()
    except Exception:
        # FTS5 MATCH can raise on malformed queries; degrade gracefully
        return []

    return [FTSHit(chunk_id=r[0], rank=float(r[1] or 0), snippet=r[2] or "") for r in rows]


def _fts_escape(query: str) -> str:
    """Convert a natural-language query to an FTS5-safe expression.

    Splits on whitespace, drops empty tokens, joins with implicit AND.
    Removes FTS5 special characters that would cause parse errors.
    """
    special = set('"-:()^*~')
    tokens = [
        "".join(c for c in tok if c not in special)
        for tok in query.split()
    ]
    tokens = [t for t in tokens if t]
    return " ".join(tokens)
