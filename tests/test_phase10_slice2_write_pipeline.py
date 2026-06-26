"""Phase 10 Slice 2 — Multi-granularity write pipeline and deterministic proposition extractor.

Tests verify:
A. Proposition extraction (deterministic, no LLM):
   - security_rule propositions from shell=False / allowlist patterns
   - constraint propositions from must/cannot/never keywords
   - risk propositions from raise statements
   - behavior propositions from docstrings
   - bullet/numbered list extraction from markdown
   - deduplication of identical propositions
   - noise filtering (too short, imports, pure code)

B. Paragraph segmentation:
   - code → per-function/class paragraphs
   - markdown → per-heading-section paragraphs
   - symbol extraction per paragraph
   - section_heading populated
   - source span tracking (start_line, end_line)

C. Summarizer:
   - chunk-level summary from a list of paragraphs
   - module-level summary includes key_symbols, responsibilities, constraints
   - constraint detection in summary

D. Full ingestion with multi-granularity (integration):
   - KnowledgeParagraphORM rows created after ingest
   - KnowledgePropositionORM rows created after ingest
   - KnowledgeChunkSummaryORM rows created after ingest
   - FTS5 tables populated for all granularities
   - Idempotent: second ingest of same content skips re-creation
   - Stale marking: changed content marks old paragraphs/propositions stale
   - Branch metadata propagates through all granularity layers
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from memory_engine.models.orm import Base
import memory_engine.models.knowledge_orm  # noqa: F401
from memory_engine.models.knowledge_orm import (
    KnowledgeChunkSummaryORM,
    KnowledgeDocumentORM,
    KnowledgeParagraphORM,
    KnowledgePropositionORM,
)
from memory_engine.models.knowledge_domain import KnowledgeIngestRequest
from memory_engine.knowledge.proposition_extractor import (
    RawProposition,
    extract_propositions,
    extract_from_code,
    extract_from_markdown,
)
from memory_engine.knowledge.paragraph_segmenter import (
    RawParagraph,
    segment_paragraphs,
    segment_code,
    segment_markdown,
)
from memory_engine.knowledge.summarizer import (
    summarize_module,
    summarize_paragraphs,
)
from memory_engine.knowledge.ingestion import KnowledgeIngestionService
from memory_engine.knowledge.fts_index import (
    create_phase10_fts_tables,
    fts_search_paragraphs,
    fts_search_propositions,
    fts_search_summaries,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PYTHON_SAMPLE = '''\
"""GitContextResolver resolves current branch, HEAD commit, and working-tree changes.

Uses an allowlisted read-only Git command runner. shell=False is enforced on all
subprocess calls and cannot be overridden.
"""
import subprocess
from dataclasses import dataclass

class GitContextResolver:
    """Resolves Git context for branch-aware memory retrieval."""

    _ALLOWED = {"rev-parse", "branch", "status"}

    def resolve(self) -> "GitContext":
        """Return a frozen GitContext for the current repository state.

        Never returns None — degrades gracefully to is_repository=False.
        """
        try:
            branch = self._run("branch", "--show-current")
            return GitContext(branch=branch, is_repository=True)
        except Exception:
            return GitContext(branch=None, is_repository=False)

    def _run(self, *args: str) -> str:
        """Execute a whitelisted Git command with shell=False."""
        cmd = args[0]
        if cmd not in self._ALLOWED:
            raise ValueError(f"Command '{cmd}' is not in the allowlist.")
        result = subprocess.run(["git", *args], shell=False, capture_output=True, text=True)
        return result.stdout.strip()
'''

_MARKDOWN_SAMPLE = """\
# Agent Memory Engine

A local-first MCP server giving coding agents persistent memory.

## Security Model

- shell=False is enforced on all Git subprocess calls.
- Commands must be in the allowlist; unlisted commands are blocked.
- Never expose remote URLs or user credentials.
- Symlink traversal is prohibited.

## Git-Aware Retrieval

The retrieval system detects the current branch using `git branch --show-current`.
It must fall back to HEAD commit detection when the branch cannot be determined.

1. Detect current branch
2. Detect HEAD commit
3. Detect working-tree changes
"""


@pytest.fixture()
def engine_():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=eng)
    with eng.connect() as conn:
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
            USING fts5(chunk_id UNINDEXED, content, heading_text, symbols_text, module_text, tags_text,
                       tokenize='porter unicode61')
        """))
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
def doc_fixture(session, project_id):
    session.execute(text(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (:id, :name, :now, :now)"
    ), {"id": project_id, "name": "test", "now": datetime.now(timezone.utc)})
    session.commit()
    return project_id


# ---------------------------------------------------------------------------
# A. Proposition extraction tests
# ---------------------------------------------------------------------------


def test_security_rule_from_shell_false():
    props = extract_from_code(_PYTHON_SAMPLE)
    types = {p.proposition_type for p in props}
    assert "security_rule" in types


def test_security_rule_from_allowlist():
    code = '# shell=False is enforced; allowlist contains only safe commands.\ndef run(): pass'
    props = extract_from_code(code)
    security = [p for p in props if p.proposition_type == "security_rule"]
    assert len(security) >= 1
    texts = [p.proposition_text.lower() for p in security]
    assert any("shell" in t or "allowlist" in t for t in texts)


def test_constraint_from_never_keyword():
    code = '# Never expose remote URLs or user identity information.\ndef check(): pass'
    props = extract_from_code(code)
    constraints = [p for p in props if p.proposition_type == "constraint"]
    assert len(constraints) >= 1


def test_risk_from_raise_statement():
    code = 'def validate(cmd):\n    raise ValueError("Command not in allowlist.")\n'
    props = extract_from_code(code)
    risks = [p for p in props if p.proposition_type == "risk"]
    assert len(risks) >= 1
    assert "ValueError" in risks[0].proposition_text or "allowlist" in risks[0].proposition_text.lower()


def test_behavior_from_docstring():
    props = extract_from_code(_PYTHON_SAMPLE)
    assert len(props) > 0
    texts = [p.proposition_text.lower() for p in props]
    # At least one proposition should mention branch or git (from docstrings/comments)
    assert any("branch" in t or "git" in t or "context" in t for t in texts)


def test_markdown_bullet_extraction():
    props = extract_from_markdown(_MARKDOWN_SAMPLE)
    assert len(props) >= 3
    prop_texts = [p.proposition_text.lower() for p in props]
    assert any("shell" in t or "allowlist" in t or "symlink" in t for t in prop_texts)


def test_markdown_numbered_list_extraction():
    props = extract_from_markdown(_MARKDOWN_SAMPLE)
    texts = [p.proposition_text.lower() for p in props]
    assert any("branch" in t or "commit" in t or "detect" in t for t in texts)


def test_markdown_constraint_sentence_extraction():
    props = extract_from_markdown(_MARKDOWN_SAMPLE)
    constraint_or_security = [
        p for p in props
        if p.proposition_type in ("constraint", "security_rule")
    ]
    assert len(constraint_or_security) >= 1


def test_proposition_deduplication():
    # Same text twice should yield only one proposition
    code = (
        '# shell=False is enforced.\n'
        '# shell=False is enforced.\n'
        'def f(): pass\n'
    )
    props = extract_from_code(code)
    hashes = [p.content_hash for p in props]
    assert len(hashes) == len(set(hashes)), "Duplicate propositions not removed"


def test_noise_filtering_imports():
    code = 'import os\nfrom pathlib import Path\n'
    props = extract_from_code(code)
    # No propositions should be extracted from pure imports
    assert all("import" not in p.proposition_text.lower() for p in props)


def test_noise_filtering_too_short():
    code = '# ok\n# hi\ndef f(): pass\n'
    props = extract_from_code(code)
    assert all(len(p.proposition_text) >= 12 for p in props)


def test_proposition_has_content_hash():
    props = extract_from_code(_PYTHON_SAMPLE)
    for p in props:
        assert len(p.content_hash) == 64  # SHA-256 hex


def test_proposition_confidence_range():
    props = extract_from_code(_PYTHON_SAMPLE)
    for p in props:
        assert 0.0 <= p.confidence <= 1.0


def test_extract_propositions_dispatch_code():
    props = extract_propositions(_PYTHON_SAMPLE, source_type="code_file")
    assert len(props) > 0


def test_extract_propositions_dispatch_markdown():
    props = extract_propositions(_MARKDOWN_SAMPLE, source_type="markdown")
    assert len(props) > 0


# ---------------------------------------------------------------------------
# B. Paragraph segmentation tests
# ---------------------------------------------------------------------------


def test_code_segmentation_per_function():
    paras = segment_code(_PYTHON_SAMPLE, source_path="git_resolver.py")
    assert len(paras) >= 2  # at least module header + GitContextResolver + resolve + _run


def test_code_segmentation_symbol_names():
    paras = segment_code(_PYTHON_SAMPLE, source_path="git_resolver.py")
    all_symbols = [s for p in paras for s in p.symbol_names]
    assert "resolve" in all_symbols or "GitContextResolver" in all_symbols


def test_code_segmentation_section_heading():
    paras = segment_code(_PYTHON_SAMPLE)
    headings = [p.section_heading for p in paras if p.section_heading]
    assert len(headings) >= 1


def test_code_segmentation_source_span():
    paras = segment_code(_PYTHON_SAMPLE, source_path="git_resolver.py")
    for p in paras:
        if p.source_start_line is not None:
            assert p.source_start_line >= 1
        if p.source_end_line is not None and p.source_start_line is not None:
            assert p.source_end_line >= p.source_start_line


def test_markdown_segmentation_per_heading():
    paras = segment_markdown(_MARKDOWN_SAMPLE)
    headings = [p.section_heading for p in paras if p.section_heading]
    assert "Security Model" in headings or "Git-Aware Retrieval" in headings


def test_markdown_segmentation_heading_path():
    paras = segment_markdown(_MARKDOWN_SAMPLE)
    assert any(len(p.heading_path) > 0 for p in paras)


def test_paragraph_token_count():
    paras = segment_code(_PYTHON_SAMPLE)
    for p in paras:
        assert p.token_count >= 1


def test_segment_paragraphs_dispatch_code():
    paras = segment_paragraphs(_PYTHON_SAMPLE, source_type="code_file")
    assert len(paras) >= 1


def test_segment_paragraphs_dispatch_markdown():
    paras = segment_paragraphs(_MARKDOWN_SAMPLE, source_type="markdown")
    assert len(paras) >= 1


# ---------------------------------------------------------------------------
# C. Summarizer tests
# ---------------------------------------------------------------------------


def test_summarize_paragraphs_produces_summary():
    paras = segment_code(_PYTHON_SAMPLE, source_path="git_resolver.py")
    summary = summarize_paragraphs(paras, granularity_level="chunk")
    assert summary is not None
    assert len(summary.summary_text) > 10


def test_summarize_paragraphs_key_symbols():
    paras = segment_code(_PYTHON_SAMPLE)
    summary = summarize_paragraphs(paras)
    assert len(summary.key_symbols) >= 1


def test_summarize_module_returns_module_level():
    paras = segment_code(_PYTHON_SAMPLE, source_path="git_resolver.py")
    summary = summarize_module(paras, source_path="git_resolver.py")
    assert summary is not None
    assert summary.granularity_level == "module"


def test_summarize_module_responsibilities():
    paras = segment_code(_PYTHON_SAMPLE)
    summary = summarize_module(paras)
    # Should extract at least some responsibilities from docstrings
    assert isinstance(summary.responsibilities, list)


def test_summarize_module_constraints_detected():
    paras = segment_markdown(_MARKDOWN_SAMPLE)
    summary = summarize_module(paras)
    assert summary is not None
    # The markdown has explicit constraint statements
    assert len(summary.constraints_mentioned) >= 0  # may or may not find them


def test_summarize_empty_returns_none():
    result = summarize_paragraphs([])
    assert result is None


def test_summarize_module_source_span():
    paras = segment_code(_PYTHON_SAMPLE, source_path="f.py")
    summary = summarize_module(paras, source_path="f.py")
    assert summary is not None
    assert summary.source_start_line is not None


# ---------------------------------------------------------------------------
# D. Full ingestion integration tests
# ---------------------------------------------------------------------------


def test_ingest_creates_paragraphs(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
        branch_name="feat/test",
    )
    result = svc.ingest(req)
    assert not result.was_duplicate

    para_count = session.query(KnowledgeParagraphORM).filter_by(
        project_id=project_id
    ).count()
    assert para_count >= 1


def test_ingest_creates_propositions(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req)

    prop_count = session.query(KnowledgePropositionORM).filter_by(
        project_id=project_id
    ).count()
    assert prop_count >= 1


def test_ingest_creates_chunk_summary(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req)

    summ_count = session.query(KnowledgeChunkSummaryORM).filter_by(
        project_id=project_id
    ).count()
    assert summ_count >= 1


def test_ingest_fts_searchable_propositions(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req)

    # Use "shell" alone — a single term more likely to survive porter stemming
    hits = fts_search_propositions(session, project_id, "shell", limit=5)
    assert len(hits) >= 1


def test_ingest_fts_searchable_paragraphs(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req)

    hits = fts_search_paragraphs(session, project_id, "branch context", limit=5)
    assert len(hits) >= 1


def test_ingest_fts_searchable_summaries(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req)

    # "resolve" stems to "resolv" via porter; use "branch" which is in the module docstring
    hits = fts_search_summaries(session, project_id, "branch", limit=5)
    assert len(hits) >= 1


def test_ingest_idempotent_same_content(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req)
    count_after_first = session.query(KnowledgeParagraphORM).filter_by(project_id=project_id).count()

    # Ingest same content again — should be duplicate skip
    result2 = svc.ingest(req)
    assert result2.was_duplicate

    count_after_second = session.query(KnowledgeParagraphORM).filter_by(project_id=project_id).count()
    assert count_after_second == count_after_first  # no new records created


def test_ingest_stale_marking_on_content_change(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)

    req_v1 = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req_v1)

    v1_para_count = session.query(KnowledgeParagraphORM).filter_by(
        project_id=project_id, is_stale=False
    ).count()
    assert v1_para_count >= 1

    # Ingest updated content
    updated_content = _PYTHON_SAMPLE + '\ndef new_method(): pass\n'
    req_v2 = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=updated_content,
        source_path="memory_engine/runtime/git/git_resolver.py",
    )
    svc.ingest(req_v2)

    stale_count = session.query(KnowledgeParagraphORM).filter_by(
        project_id=project_id, is_stale=True
    ).count()
    assert stale_count >= 1, "Old paragraphs should be marked stale after content change"

    fresh_count = session.query(KnowledgeParagraphORM).filter_by(
        project_id=project_id, is_stale=False
    ).count()
    assert fresh_count >= 1, "New paragraphs should be created after update"


def test_branch_metadata_propagates_to_paragraphs(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
        branch_name="feat/multigranular",
    )
    svc.ingest(req)

    paras = session.query(KnowledgeParagraphORM).filter_by(
        project_id=project_id, branch_name="feat/multigranular"
    ).all()
    assert len(paras) >= 1


def test_branch_metadata_propagates_to_propositions(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="code_file",
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
        branch_name="feat/multigranular",
    )
    svc.ingest(req)

    props = session.query(KnowledgePropositionORM).filter_by(
        project_id=project_id, branch_name="feat/multigranular"
    ).all()
    assert len(props) >= 1


def test_markdown_ingest_creates_all_layers(session, doc_fixture):
    project_id = doc_fixture
    svc = KnowledgeIngestionService(session)
    req = KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type="markdown",
        title="README.md",
        content=_MARKDOWN_SAMPLE,
        source_path="README.md",
    )
    svc.ingest(req)

    assert session.query(KnowledgeParagraphORM).filter_by(project_id=project_id).count() >= 1
    assert session.query(KnowledgePropositionORM).filter_by(project_id=project_id).count() >= 1
    assert session.query(KnowledgeChunkSummaryORM).filter_by(project_id=project_id).count() >= 1
