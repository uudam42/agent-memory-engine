"""Phase 13 — hybrid lexical + semantic retrieval tests."""

from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import memory_engine.models.knowledge_orm  # noqa: F401
from memory_engine.knowledge.embedding import NoEmbeddingProvider
from memory_engine.knowledge.fts_index import create_phase10_fts_tables
from memory_engine.knowledge.ingestion import KnowledgeIngestionService
from memory_engine.knowledge.search import KnowledgeSearchService
from memory_engine.models.knowledge_domain import (
    KnowledgeIngestRequest,
    KnowledgeSearchRequest,
    SourceType,
)
from memory_engine.models.orm import Base

sqlite_vec = pytest.importorskip("sqlite_vec")

from memory_engine.knowledge.sqlite_vec_index import SqliteVecIndex  # noqa: E402


class FakeProvider:
    provider_name = "fake"
    model_name = "fake-model-v1"
    dimension = 24

    def is_available(self) -> bool:
        return True

    def _embed_one(self, text_: str) -> list[float]:
        vec = [0.0] * self.dimension
        for tok in text_.lower().split():
            idx = int(hashlib.sha256(tok.encode()).hexdigest(), 16) % self.dimension
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_texts(self, texts):
        return [self._embed_one(t) for t in texts]

    def embed_query(self, query):
        return self._embed_one(query)


_DOC = """\
# Persistence Layer

The persistence layer stores records in a relational database with schema
migrations applied at startup. Each migration is idempotent and safe to re-run.
"""


@pytest.fixture()
def engine_():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=eng)
    with eng.connect() as conn:
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
            USING fts5(chunk_id UNINDEXED, content, heading_text, symbols_text,
                       module_text, tags_text, tokenize='porter unicode61')
        """))
        create_phase10_fts_tables(conn)
        conn.commit()
    return eng


@pytest.fixture()
def session(engine_):
    with Session(engine_) as sess:
        yield sess


@pytest.fixture()
def project_id():
    return str(uuid.uuid4())


def _make_project(session, project_id):
    session.execute(text(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (:id, :name, :now, :now)"
    ), {"id": project_id, "name": "test", "now": datetime.now(timezone.utc)})
    session.commit()


def test_no_provider_keeps_lexical_behaviour(session, project_id):
    _make_project(session, project_id)
    svc = KnowledgeIngestionService(session)  # no semantic index
    svc.ingest(KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type=SourceType.markdown,
        title="persist.md",
        content=_DOC,
        source_path="docs/persist.md",
    ))
    search = KnowledgeSearchService(session)  # ephemeral BoW path
    results = search.search(KnowledgeSearchRequest(
        project_id=uuid.UUID(project_id),
        query="database schema migrations",
        max_results=5,
        token_budget=4000,
    ))
    assert results
    # No persistent semantic backend → semantic_similarity stays 0.0.
    assert results[0].score_breakdown.get("semantic_similarity", 0.0) == 0.0


def test_hybrid_produces_nonzero_semantic(tmp_path, session, project_id):
    _make_project(session, project_id)
    index = SqliteVecIndex(tmp_path / "vector.db", FakeProvider())
    ing = KnowledgeIngestionService(session, semantic_index=index)
    ing.ingest(KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type=SourceType.markdown,
        title="persist.md",
        content=_DOC,
        source_path="docs/persist.md",
    ))
    search = KnowledgeSearchService(session, semantic_index=index)
    results = search.search(KnowledgeSearchRequest(
        project_id=uuid.UUID(project_id),
        query="database migrations schema startup",
        max_results=5,
        token_budget=4000,
    ))
    assert results
    sims = [r.score_breakdown.get("semantic_similarity", 0.0) for r in results]
    assert max(sims) > 0.0


def test_rrf_fusion_is_deterministic(tmp_path, session, project_id):
    _make_project(session, project_id)
    index = SqliteVecIndex(tmp_path / "vector.db", FakeProvider())
    ing = KnowledgeIngestionService(session, semantic_index=index)
    ing.ingest(KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type=SourceType.markdown,
        title="persist.md",
        content=_DOC,
        source_path="docs/persist.md",
    ))

    def _run():
        s = KnowledgeSearchService(
            session, semantic_index=index, cache=_NoCache()
        )
        return [
            (str(r.chunk_id), round(r.score, 6))
            for r in s.search(KnowledgeSearchRequest(
                project_id=uuid.UUID(project_id),
                query="idempotent migration database",
                max_results=5,
                token_budget=4000,
            ))
        ]

    assert _run() == _run()


def test_stale_records_excluded_from_semantic(tmp_path, session, project_id):
    _make_project(session, project_id)
    index = SqliteVecIndex(tmp_path / "vector.db", FakeProvider())
    index.upsert("manual-chunk", "database schema migration topic", {
        "project_id": project_id,
        "record_type": "chunk",
        "lifecycle_state": "stale",
        "branch_scope": "global",
    })
    results = index.search("database schema migration", project_id, limit=5)
    assert all(r.record_id != "manual-chunk" for r in results)


class _NoCache:
    def get(self, key):
        return None

    def set(self, key, value, project_id):
        pass

    @staticmethod
    def make_key(**kwargs):
        return repr(sorted(kwargs.items()))
