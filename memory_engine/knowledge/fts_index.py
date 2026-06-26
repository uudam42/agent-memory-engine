"""FTS5 lexical index for multi-granularity knowledge retrieval (Phase 10).

Tables:
  knowledge_chunks_fts       — original chunk-level FTS5 (Phase 6)
  knowledge_paragraphs_fts   — paragraph-level FTS5 (Phase 10)
  knowledge_propositions_fts — proposition-level FTS5 (Phase 10)
  knowledge_summaries_fts    — chunk/module summary FTS5 (Phase 10)

All tables are created by init_db.create_fts_tables() (IF NOT EXISTS — safe
to call multiple times).

Phase 10 write functions:
  paragraph_fts_insert / paragraph_fts_delete
  proposition_fts_insert / proposition_fts_delete
  summary_fts_insert / summary_fts_delete

Phase 10 search functions:
  fts_search_paragraphs / fts_search_propositions / fts_search_summaries
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


# ---------------------------------------------------------------------------
# Phase 10: Paragraph FTS5
# ---------------------------------------------------------------------------

_DDL_PARAGRAPHS_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_paragraphs_fts
USING fts5(
    paragraph_id UNINDEXED,
    content,
    summary,
    section_heading,
    symbols_text,
    tokenize = 'porter unicode61'
);
"""

_DDL_PROPOSITIONS_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_propositions_fts
USING fts5(
    proposition_id UNINDEXED,
    proposition_text,
    proposition_type UNINDEXED,
    tokenize = 'porter unicode61'
);
"""

_DDL_SUMMARIES_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_summaries_fts
USING fts5(
    summary_id UNINDEXED,
    summary_text,
    purpose,
    symbols_text,
    granularity_level UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def create_phase10_fts_tables(conn) -> None:  # type: ignore[type-arg]
    """Create Phase 10 FTS5 virtual tables. Safe to call multiple times."""
    conn.execute(text(_DDL_PARAGRAPHS_FTS5))
    conn.execute(text(_DDL_PROPOSITIONS_FTS5))
    conn.execute(text(_DDL_SUMMARIES_FTS5))


def paragraph_fts_insert(
    session: Session,
    paragraph_id: str,
    content: str,
    summary: str | None,
    section_heading: str | None,
    symbol_names: list[str],
) -> None:
    session.execute(text(
        "INSERT INTO knowledge_paragraphs_fts"
        " (paragraph_id, content, summary, section_heading, symbols_text)"
        " VALUES (:pid, :content, :summary, :heading, :symbols)"
    ), {
        "pid": paragraph_id,
        "content": content[:8192],
        "summary": (summary or "")[:1024],
        "heading": section_heading or "",
        "symbols": " ".join(symbol_names),
    })


def paragraph_fts_delete(session: Session, paragraph_id: str) -> None:
    session.execute(
        text("DELETE FROM knowledge_paragraphs_fts WHERE paragraph_id = :pid"),
        {"pid": paragraph_id},
    )


def proposition_fts_insert(
    session: Session,
    proposition_id: str,
    proposition_text: str,
    proposition_type: str,
) -> None:
    session.execute(text(
        "INSERT INTO knowledge_propositions_fts"
        " (proposition_id, proposition_text, proposition_type)"
        " VALUES (:pid, :text, :ptype)"
    ), {
        "pid": proposition_id,
        "text": proposition_text[:4096],
        "ptype": proposition_type,
    })


def proposition_fts_delete(session: Session, proposition_id: str) -> None:
    session.execute(
        text("DELETE FROM knowledge_propositions_fts WHERE proposition_id = :pid"),
        {"pid": proposition_id},
    )


def summary_fts_insert(
    session: Session,
    summary_id: str,
    summary_text: str,
    purpose: str | None,
    key_symbols: list[str],
    granularity_level: str,
) -> None:
    session.execute(text(
        "INSERT INTO knowledge_summaries_fts"
        " (summary_id, summary_text, purpose, symbols_text, granularity_level)"
        " VALUES (:sid, :text, :purpose, :symbols, :level)"
    ), {
        "sid": summary_id,
        "text": summary_text[:4096],
        "purpose": (purpose or "")[:1024],
        "symbols": " ".join(key_symbols),
        "level": granularity_level,
    })


def summary_fts_delete(session: Session, summary_id: str) -> None:
    session.execute(
        text("DELETE FROM knowledge_summaries_fts WHERE summary_id = :sid"),
        {"sid": summary_id},
    )


@dataclass
class FTSParagraphHit:
    paragraph_id: str
    rank: float
    snippet: str


@dataclass
class FTSPropositionHit:
    proposition_id: str
    rank: float


@dataclass
class FTSSummaryHit:
    summary_id: str
    rank: float
    granularity_level: str


def fts_search_paragraphs(
    session: Session,
    project_id: str,
    query: str,
    limit: int = 20,
) -> list[FTSParagraphHit]:
    """FTS5 search over paragraphs, filtered by project."""
    safe_query = _fts_escape(query)
    if not safe_query:
        return []
    try:
        rows = session.execute(text("""
            SELECT f.paragraph_id, f.rank,
                   snippet(knowledge_paragraphs_fts, 1, '<b>', '</b>', '...', 20) AS snippet
            FROM   knowledge_paragraphs_fts f
            JOIN   knowledge_paragraphs p ON p.paragraph_id = f.paragraph_id
            WHERE  knowledge_paragraphs_fts MATCH :q
              AND  p.project_id = :pid
              AND  p.is_stale = 0
            ORDER BY f.rank
            LIMIT  :lim
        """), {"q": safe_query, "pid": project_id, "lim": limit}).fetchall()
    except Exception:
        return []
    return [FTSParagraphHit(paragraph_id=r[0], rank=float(r[1] or 0), snippet=r[2] or "") for r in rows]


def fts_search_propositions(
    session: Session,
    project_id: str,
    query: str,
    limit: int = 20,
) -> list[FTSPropositionHit]:
    """FTS5 search over propositions, filtered by project."""
    safe_query = _fts_escape(query)
    if not safe_query:
        return []
    try:
        rows = session.execute(text("""
            SELECT f.proposition_id, f.rank
            FROM   knowledge_propositions_fts f
            JOIN   knowledge_propositions p ON p.proposition_id = f.proposition_id
            WHERE  knowledge_propositions_fts MATCH :q
              AND  p.project_id = :pid
              AND  p.is_stale = 0
            ORDER BY f.rank
            LIMIT  :lim
        """), {"q": safe_query, "pid": project_id, "lim": limit}).fetchall()
    except Exception:
        return []
    return [FTSPropositionHit(proposition_id=r[0], rank=float(r[1] or 0)) for r in rows]


def fts_search_summaries(
    session: Session,
    project_id: str,
    query: str,
    granularity_level: str | None = None,
    limit: int = 20,
) -> list[FTSSummaryHit]:
    """FTS5 search over chunk/module summaries, filtered by project."""
    safe_query = _fts_escape(query)
    if not safe_query:
        return []
    level_filter = "AND s.granularity_level = :level" if granularity_level else ""
    try:
        rows = session.execute(text(f"""
            SELECT f.summary_id, f.rank, s.granularity_level
            FROM   knowledge_summaries_fts f
            JOIN   knowledge_chunk_summaries s ON s.summary_id = f.summary_id
            WHERE  knowledge_summaries_fts MATCH :q
              AND  s.project_id = :pid
              AND  s.is_stale = 0
              {level_filter}
            ORDER BY f.rank
            LIMIT  :lim
        """), {"q": safe_query, "pid": project_id, "lim": limit,
               "level": granularity_level or ""}).fetchall()
    except Exception:
        return []
    return [FTSSummaryHit(summary_id=r[0], rank=float(r[1] or 0), granularity_level=r[2] or "chunk") for r in rows]


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
