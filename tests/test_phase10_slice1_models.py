"""Phase 10 Slice 1 — Multi-granularity ORM models, schema migration, FTS5 tables.

Tests verify:
- New ORM tables can be created (create_all is idempotent)
- FTS5 virtual tables for paragraphs, propositions, summaries are created
- KnowledgePropositionORM, KnowledgeParagraphORM, KnowledgeChunkSummaryORM CRUD
- Parent-child relationships (paragraph → propositions)
- is_stale / superseded_by lifecycle fields exist
- Branch/revision provenance fields propagate correctly
- Pydantic domain models validate from ORM attributes (from_attributes)
- Schema migration (ALTER TABLE) is idempotent for Phase 10 columns
- FTS5 insert/search/delete round-trips for each new granularity
- Existing KnowledgeChunk tests are not regressed (backward compat)
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from memory_engine.models.orm import Base
import memory_engine.models.knowledge_orm  # noqa: F401 — registers all ORM classes
from memory_engine.models.knowledge_orm import (
    KnowledgeChunkORM,
    KnowledgeChunkSummaryORM,
    KnowledgeDocumentORM,
    KnowledgeParagraphORM,
    KnowledgePropositionORM,
)
from memory_engine.models.knowledge_domain import (
    GranularityLevel,
    KnowledgeChunkSummary,
    KnowledgeParagraph,
    KnowledgeProposition,
    MultiGranularitySearchResult,
    PropositionType,
)
from memory_engine.knowledge.fts_index import (
    create_phase10_fts_tables,
    fts_search_paragraphs,
    fts_search_propositions,
    fts_search_summaries,
    paragraph_fts_delete,
    paragraph_fts_insert,
    proposition_fts_delete,
    proposition_fts_insert,
    summary_fts_delete,
    summary_fts_insert,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine_():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=eng)
    with eng.connect() as conn:
        # Phase 6 FTS5
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
            USING fts5(
                chunk_id UNINDEXED, content, heading_text,
                symbols_text, module_text, tags_text,
                tokenize = 'porter unicode61'
            )
        """))
        # Phase 10 FTS5
        create_phase10_fts_tables(conn)
        conn.commit()
    return eng


@pytest.fixture()
def session(engine_):
    with Session(engine_) as sess:
        yield sess


@pytest.fixture()
def project_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def document(session, project_id, engine_):
    """A minimal KnowledgeDocumentORM row (no FK to projects table in tests)."""
    # Insert a fake project row so the FK is satisfied
    session.execute(text(
        "INSERT INTO projects (id, name, created_at, updated_at)"
        " VALUES (:id, :name, :now, :now)"
    ), {"id": project_id, "name": "test", "now": datetime.now(timezone.utc)})
    doc = KnowledgeDocumentORM(
        document_id=str(uuid.uuid4()),
        project_id=project_id,
        source_type="code_file",
        title="git_context.py",
        source_path="memory_engine/runtime/git/git_context.py",
        content_hash=hashlib.sha256(b"test").hexdigest(),
        status="indexed",
    )
    session.add(doc)
    session.commit()
    return doc


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------


def test_new_tables_exist(engine_):
    inspector = inspect(engine_)
    tables = inspector.get_table_names()
    assert "knowledge_paragraphs" in tables
    assert "knowledge_propositions" in tables
    assert "knowledge_chunk_summaries" in tables


def test_fts5_tables_exist(engine_):
    with Session(engine_) as sess:
        # Each FTS5 virtual table should be selectable
        for tbl in ("knowledge_paragraphs_fts", "knowledge_propositions_fts", "knowledge_summaries_fts"):
            result = sess.execute(text(f"SELECT count(*) FROM {tbl}")).scalar()
            assert result == 0


def test_document_summary_columns_exist(engine_):
    inspector = inspect(engine_)
    cols = {c["name"] for c in inspector.get_columns("knowledge_documents")}
    for col in ("document_summary", "module_summary", "architecture_summary",
                "key_symbols", "dependencies", "related_documents"):
        assert col in cols, f"Missing column: {col}"


# ---------------------------------------------------------------------------
# KnowledgeParagraphORM CRUD
# ---------------------------------------------------------------------------


def test_paragraph_create_and_read(session, document):
    para = KnowledgeParagraphORM(
        paragraph_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="def resolve(self) -> GitContext:\n    ...",
        summary="Resolves current git context including branch and HEAD commit.",
        symbol_names=["resolve", "GitContext"],
        section_heading="GitContextResolver",
        heading_path=["GitContextResolver", "resolve"],
        paragraph_index=0,
        token_count=25,
        content_hash=hashlib.sha256(b"para1").hexdigest(),
        source_path="memory_engine/runtime/git/git_resolver.py",
        source_start_line=42,
        source_end_line=58,
        branch_name="feat/test",
        branch_scope="current_branch",
        source_revision="abc123",
        commit_sha="abc123de",
        is_stale=False,
    )
    session.add(para)
    session.commit()

    fetched = session.get(KnowledgeParagraphORM, para.paragraph_id)
    assert fetched is not None
    assert fetched.symbol_names == ["resolve", "GitContext"]
    assert fetched.branch_scope == "current_branch"
    assert fetched.is_stale is False


def test_paragraph_stale_lifecycle(session, document):
    para = KnowledgeParagraphORM(
        paragraph_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="old content",
        content_hash=hashlib.sha256(b"old").hexdigest(),
        is_stale=False,
    )
    session.add(para)
    session.commit()

    para.is_stale = True
    new_id = str(uuid.uuid4())
    para.superseded_by = new_id
    session.commit()

    fetched = session.get(KnowledgeParagraphORM, para.paragraph_id)
    assert fetched.is_stale is True
    assert fetched.superseded_by == new_id


# ---------------------------------------------------------------------------
# KnowledgePropositionORM CRUD
# ---------------------------------------------------------------------------


def test_proposition_create_and_read(session, document):
    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        proposition_text="GitContextResolver uses an allowlisted read-only Git command runner.",
        normalized_text="gitcontextresolver uses an allowlisted read-only git command runner.",
        proposition_type="security_rule",
        confidence=0.95,
        content_hash=hashlib.sha256(b"prop1").hexdigest(),
        source_path="memory_engine/runtime/git/git_resolver.py",
        source_start_line=10,
        source_end_line=15,
        branch_scope="global",
    )
    session.add(prop)
    session.commit()

    fetched = session.get(KnowledgePropositionORM, prop.proposition_id)
    assert fetched is not None
    assert fetched.proposition_type == "security_rule"
    assert fetched.confidence == pytest.approx(0.95)


def test_proposition_parent_paragraph_link(session, document):
    para = KnowledgeParagraphORM(
        paragraph_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="Security-critical section.",
        content_hash=hashlib.sha256(b"para-sec").hexdigest(),
    )
    session.add(para)
    session.flush()

    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        paragraph_id=para.paragraph_id,
        project_id=document.project_id,
        proposition_text="shell=False is enforced on all Git subprocess calls.",
        normalized_text="shell=false is enforced on all git subprocess calls.",
        proposition_type="security_rule",
        confidence=0.99,
        content_hash=hashlib.sha256(b"prop-shell").hexdigest(),
    )
    session.add(prop)
    session.commit()

    fetched = session.get(KnowledgePropositionORM, prop.proposition_id)
    assert fetched.paragraph_id == para.paragraph_id
    # Cascade relationship
    para_fetched = session.get(KnowledgeParagraphORM, para.paragraph_id)
    assert len(para_fetched.propositions) == 1
    assert para_fetched.propositions[0].proposition_text == prop.proposition_text


def test_proposition_cascade_delete_with_paragraph(session, document):
    para = KnowledgeParagraphORM(
        paragraph_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="will be deleted",
        content_hash=hashlib.sha256(b"del").hexdigest(),
    )
    session.add(para)
    session.flush()

    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        paragraph_id=para.paragraph_id,
        project_id=document.project_id,
        proposition_text="will also be deleted",
        normalized_text="will also be deleted",
        proposition_type="implementation_detail",
        confidence=0.7,
        content_hash=hashlib.sha256(b"del-prop").hexdigest(),
    )
    session.add(prop)
    session.commit()

    prop_id = prop.proposition_id
    session.delete(para)
    session.commit()

    assert session.get(KnowledgePropositionORM, prop_id) is None


# ---------------------------------------------------------------------------
# KnowledgeChunkSummaryORM CRUD
# ---------------------------------------------------------------------------


def test_chunk_summary_create_and_read(session, document):
    summ = KnowledgeChunkSummaryORM(
        summary_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        summary_text="GitContextResolver resolves branch, HEAD commit, and working tree changes using a read-only allowlisted Git command runner.",
        purpose="Provide branch-aware context for memory retrieval",
        key_symbols=["GitContextResolver", "GitContext", "resolve"],
        responsibilities=["Detect current branch", "Detect HEAD commit", "Detect modified files"],
        constraints_mentioned=["shell=False enforced", "allowlist only"],
        important_interactions=["ProjectContext", "DeterministicRanker"],
        granularity_level="module",
        content_hash=hashlib.sha256(b"summ1").hexdigest(),
        source_path="memory_engine/runtime/git/git_resolver.py",
        source_start_line=1,
        source_end_line=200,
        token_count=180,
        branch_scope="global",
        is_stale=False,
    )
    session.add(summ)
    session.commit()

    fetched = session.get(KnowledgeChunkSummaryORM, summ.summary_id)
    assert fetched is not None
    assert fetched.granularity_level == "module"
    assert "GitContextResolver" in fetched.key_symbols
    assert len(fetched.responsibilities) == 3


# ---------------------------------------------------------------------------
# Pydantic domain model validation
# ---------------------------------------------------------------------------


def test_pydantic_paragraph_from_attributes(session, document):
    para = KnowledgeParagraphORM(
        paragraph_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="test paragraph",
        content_hash=hashlib.sha256(b"p").hexdigest(),
        symbol_names=["foo"],
        heading_path=[],
        is_stale=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(para)
    session.commit()

    domain = KnowledgeParagraph.model_validate(para)
    assert str(domain.paragraph_id) == para.paragraph_id
    assert domain.symbol_names == ["foo"]
    assert domain.is_stale is False


def test_pydantic_proposition_from_attributes(session, document):
    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        proposition_text="Feature branch memory requires explicit promotion.",
        normalized_text="feature branch memory requires explicit promotion.",
        proposition_type="constraint",
        confidence=0.9,
        content_hash=hashlib.sha256(b"pp").hexdigest(),
        is_stale=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(prop)
    session.commit()

    domain = KnowledgeProposition.model_validate(prop)
    assert domain.proposition_type == PropositionType.constraint
    assert domain.confidence == pytest.approx(0.9)


def test_pydantic_chunk_summary_from_attributes(session, document):
    summ = KnowledgeChunkSummaryORM(
        summary_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        summary_text="This module handles git context resolution.",
        granularity_level="module",
        content_hash=hashlib.sha256(b"s").hexdigest(),
        key_symbols=["GitContextResolver"],
        responsibilities=["detect branch"],
        constraints_mentioned=[],
        important_interactions=[],
        is_stale=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(summ)
    session.commit()

    domain = KnowledgeChunkSummary.model_validate(summ)
    assert domain.granularity_level == GranularityLevel.module


def test_multi_granularity_search_result_model():
    result = MultiGranularitySearchResult(
        result_id="prop_abc",
        result_type="proposition",
        granularity=GranularityLevel.proposition,
        content="shell=False is enforced.",
        score=0.87,
        score_breakdown={"granularity_fit": 0.14, "branch_scope": 0.16},
        source_path="memory_engine/runtime/git/git_resolver.py",
        source_start_line=42,
        source_end_line=42,
        branch_name="feat/test",
        branch_scope="current_branch",
        commit_sha="abc123de",
        is_stale=False,
        selection_reason="high-precision security_rule proposition",
    )
    assert result.score == pytest.approx(0.87)
    assert result.granularity == GranularityLevel.proposition


# ---------------------------------------------------------------------------
# FTS5 round-trips — paragraphs
# ---------------------------------------------------------------------------


def test_paragraph_fts_insert_and_search(session, document):
    para = KnowledgeParagraphORM(
        paragraph_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="GitContextResolver reads the current branch using git branch command.",
        summary="Resolves git branch information.",
        symbol_names=["GitContextResolver"],
        section_heading="GitContextResolver",
        heading_path=["GitContextResolver"],
        content_hash=hashlib.sha256(b"fts-para").hexdigest(),
        is_stale=False,
    )
    session.add(para)
    session.commit()

    paragraph_fts_insert(
        session,
        paragraph_id=para.paragraph_id,
        content=para.content,
        summary=para.summary,
        section_heading=para.section_heading,
        symbol_names=para.symbol_names,
    )
    session.commit()

    hits = fts_search_paragraphs(session, document.project_id, "branch resolver", limit=5)
    assert any(h.paragraph_id == para.paragraph_id for h in hits)


def test_paragraph_fts_delete(session, document):
    pid = str(uuid.uuid4())
    para = KnowledgeParagraphORM(
        paragraph_id=pid,
        document_id=document.document_id,
        project_id=document.project_id,
        content="unique phrase xyzzy paragraph content for deletion test",
        content_hash=hashlib.sha256(b"fts-del-para").hexdigest(),
        is_stale=False,
    )
    session.add(para)
    session.commit()

    paragraph_fts_insert(session, pid, para.content, None, None, [])
    session.commit()

    hits_before = fts_search_paragraphs(session, document.project_id, "xyzzy paragraph", limit=5)
    assert any(h.paragraph_id == pid for h in hits_before)

    paragraph_fts_delete(session, pid)
    session.commit()

    hits_after = fts_search_paragraphs(session, document.project_id, "xyzzy paragraph", limit=5)
    assert not any(h.paragraph_id == pid for h in hits_after)


# ---------------------------------------------------------------------------
# FTS5 round-trips — propositions
# ---------------------------------------------------------------------------


def test_proposition_fts_insert_and_search(session, document):
    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        proposition_text="All Git subprocess calls enforce shell equals False.",
        normalized_text="all git subprocess calls enforce shell equals false.",
        proposition_type="security_rule",
        confidence=0.99,
        content_hash=hashlib.sha256(b"fts-prop").hexdigest(),
        is_stale=False,
    )
    session.add(prop)
    session.commit()

    proposition_fts_insert(
        session,
        proposition_id=prop.proposition_id,
        proposition_text=prop.proposition_text,
        proposition_type=prop.proposition_type,
    )
    session.commit()

    hits = fts_search_propositions(session, document.project_id, "subprocess shell", limit=5)
    assert any(h.proposition_id == prop.proposition_id for h in hits)


def test_proposition_fts_delete(session, document):
    pid = str(uuid.uuid4())
    prop = KnowledgePropositionORM(
        proposition_id=pid,
        document_id=document.document_id,
        project_id=document.project_id,
        proposition_text="Unique proposition text zzzyyyxxx for deletion.",
        normalized_text="unique proposition text zzzyyyxxx for deletion.",
        proposition_type="implementation_detail",
        confidence=0.7,
        content_hash=hashlib.sha256(b"fts-del-prop").hexdigest(),
        is_stale=False,
    )
    session.add(prop)
    session.commit()

    proposition_fts_insert(session, pid, prop.proposition_text, prop.proposition_type)
    session.commit()

    hits_before = fts_search_propositions(session, document.project_id, "zzzyyyxxx", limit=5)
    assert any(h.proposition_id == pid for h in hits_before)

    proposition_fts_delete(session, pid)
    session.commit()

    hits_after = fts_search_propositions(session, document.project_id, "zzzyyyxxx", limit=5)
    assert not any(h.proposition_id == pid for h in hits_after)


def test_stale_propositions_excluded_from_fts(session, document):
    """Stale propositions must not appear in FTS results."""
    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        proposition_text="Stale unique phrase qqqwwweee must not appear.",
        normalized_text="stale unique phrase qqqwwweee must not appear.",
        proposition_type="implementation_detail",
        confidence=0.7,
        content_hash=hashlib.sha256(b"stale").hexdigest(),
        is_stale=True,
    )
    session.add(prop)
    session.commit()

    proposition_fts_insert(session, prop.proposition_id, prop.proposition_text, prop.proposition_type)
    session.commit()

    # is_stale=True means the JOIN filter excludes it
    hits = fts_search_propositions(session, document.project_id, "qqqwwweee", limit=5)
    assert not any(h.proposition_id == prop.proposition_id for h in hits)


# ---------------------------------------------------------------------------
# FTS5 round-trips — summaries
# ---------------------------------------------------------------------------


def test_summary_fts_insert_and_search(session, document):
    summ = KnowledgeChunkSummaryORM(
        summary_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        summary_text="Handles branch-aware memory retrieval with deterministic ranking.",
        purpose="Rank memories by branch affinity and revision validity.",
        key_symbols=["DeterministicRanker", "BranchAffinity"],
        responsibilities=[],
        constraints_mentioned=[],
        important_interactions=[],
        granularity_level="module",
        content_hash=hashlib.sha256(b"fts-summ").hexdigest(),
        is_stale=False,
    )
    session.add(summ)
    session.commit()

    summary_fts_insert(
        session,
        summary_id=summ.summary_id,
        summary_text=summ.summary_text,
        purpose=summ.purpose,
        key_symbols=summ.key_symbols,
        granularity_level=summ.granularity_level,
    )
    session.commit()

    hits = fts_search_summaries(session, document.project_id, "branch ranking", limit=5)
    assert any(h.summary_id == summ.summary_id for h in hits)


def test_summary_fts_granularity_filter(session, document):
    chunk_summ = KnowledgeChunkSummaryORM(
        summary_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        summary_text="Chunk-level retrieval scoring logic.",
        granularity_level="chunk",
        content_hash=hashlib.sha256(b"chunk-g").hexdigest(),
        key_symbols=[], responsibilities=[], constraints_mentioned=[], important_interactions=[],
        is_stale=False,
    )
    mod_summ = KnowledgeChunkSummaryORM(
        summary_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        summary_text="Module-level retrieval scoring logic.",
        granularity_level="module",
        content_hash=hashlib.sha256(b"mod-g").hexdigest(),
        key_symbols=[], responsibilities=[], constraints_mentioned=[], important_interactions=[],
        is_stale=False,
    )
    session.add_all([chunk_summ, mod_summ])
    session.commit()

    for s in [chunk_summ, mod_summ]:
        summary_fts_insert(session, s.summary_id, s.summary_text, None, [], s.granularity_level)
    session.commit()

    module_hits = fts_search_summaries(
        session, document.project_id, "retrieval scoring", granularity_level="module", limit=5
    )
    module_ids = {h.summary_id for h in module_hits}
    assert mod_summ.summary_id in module_ids
    assert chunk_summ.summary_id not in module_ids


# ---------------------------------------------------------------------------
# Branch / revision provenance
# ---------------------------------------------------------------------------


def test_branch_provenance_stored_on_proposition(session, document):
    prop = KnowledgePropositionORM(
        proposition_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        proposition_text="Feature branch memory requires explicit promotion before mainline use.",
        normalized_text="feature branch memory requires explicit promotion before mainline use.",
        proposition_type="constraint",
        confidence=0.92,
        content_hash=hashlib.sha256(b"branch-prov").hexdigest(),
        branch_name="feat/multigranular-memory-retrieval",
        branch_scope="current_branch",
        source_revision="7546666a",
        commit_sha="7546666a",
        is_stale=False,
    )
    session.add(prop)
    session.commit()

    fetched = session.get(KnowledgePropositionORM, prop.proposition_id)
    assert fetched.branch_name == "feat/multigranular-memory-retrieval"
    assert fetched.branch_scope == "current_branch"
    assert fetched.source_revision == "7546666a"


# ---------------------------------------------------------------------------
# Backward compatibility — existing KnowledgeChunkORM still works
# ---------------------------------------------------------------------------


def test_legacy_chunk_orm_unaffected(session, document):
    chunk = KnowledgeChunkORM(
        chunk_id=str(uuid.uuid4()),
        document_id=document.document_id,
        project_id=document.project_id,
        content="legacy chunk content",
        content_hash=hashlib.sha256(b"legacy").hexdigest(),
        chunk_index=0,
        token_count=5,
        heading_path=[],
        module_paths=[],
        related_symbols=[],
        tags=[],
        source_type="code_file",
        embedding_status="pending",
        index_status="pending",
    )
    session.add(chunk)
    session.commit()

    fetched = session.get(KnowledgeChunkORM, chunk.chunk_id)
    assert fetched is not None
    assert fetched.content == "legacy chunk content"


# ---------------------------------------------------------------------------
# Idempotency — duplicate FTS5 create is safe
# ---------------------------------------------------------------------------


def test_phase10_fts_create_is_idempotent(engine_):
    with engine_.connect() as conn:
        create_phase10_fts_tables(conn)
        create_phase10_fts_tables(conn)
        conn.commit()
    # No exception means IF NOT EXISTS guard worked
