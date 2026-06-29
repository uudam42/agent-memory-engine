"""Phase 13 — sqlite-vec persistent backend tests.

Skipped entirely when sqlite_vec is not importable.
"""

from __future__ import annotations

import hashlib
import math

import pytest

pytest.importorskip("sqlite_vec")

from memory_engine.knowledge.sqlite_vec_index import (  # noqa: E402
    SqliteVecIndex,
    VectorSearchResult,
)


class FakeProvider:
    """Deterministic embedding provider: hashes tokens into a fixed-dim unit vector."""

    provider_name = "fake"
    model_name = "fake-model-v1"
    dimension = 16

    def is_available(self) -> bool:
        return True

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for tok in text.lower().split():
            idx = int(hashlib.sha256(tok.encode()).hexdigest(), 16) % self.dimension
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_texts(self, texts):
        return [self._embed_one(t) for t in texts]

    def embed_query(self, query):
        return self._embed_one(query)


def _meta(project_id="p1", record_type="chunk", lifecycle="active", branch=None):
    return {
        "project_id": project_id,
        "record_type": record_type,
        "lifecycle_state": lifecycle,
        "branch_name": branch,
        "branch_scope": "global",
        "source_path": "x.py",
    }


def _index(tmp_path):
    return SqliteVecIndex(tmp_path / "vector.db", FakeProvider())


def test_init_creates_tables(tmp_path):
    idx = _index(tmp_path)
    conn = idx._get_conn()
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "vec_metadata" in tables


def test_upsert_then_search(tmp_path):
    idx = _index(tmp_path)
    assert idx.upsert("c1", "database migration schema", _meta()) is True
    assert idx.upsert("c2", "unrelated rendering pixels", _meta()) is True
    results = idx.search("database schema", "p1", limit=5)
    assert results
    assert results[0].record_id == "c1"
    assert results[0].score > 0.0


def test_idempotent_upsert_skips_reembed(tmp_path):
    idx = _index(tmp_path)
    assert idx.upsert("c1", "same content", _meta()) is True
    # Same content + model → cache hit, returns False.
    assert idx.upsert("c1", "same content", _meta()) is False


def test_changed_content_reembeds(tmp_path):
    idx = _index(tmp_path)
    assert idx.upsert("c1", "first content", _meta()) is True
    assert idx.upsert("c1", "second different content", _meta()) is True
    stats = idx.get_stats("p1")
    assert stats["embedded_count"] == 1  # still one row, updated in place


def test_project_isolation(tmp_path):
    idx = _index(tmp_path)
    idx.upsert("a", "shared topic alpha", _meta(project_id="p1"))
    idx.upsert("b", "shared topic alpha", _meta(project_id="p2"))
    results = idx.search("shared topic alpha", "p1", limit=10)
    ids = {r.record_id for r in results}
    assert ids == {"a"}


def test_lifecycle_filter_excludes_stale(tmp_path):
    idx = _index(tmp_path)
    idx.upsert("active1", "lifecycle topic", _meta(lifecycle="active"))
    idx.upsert("stale1", "lifecycle topic", _meta(lifecycle="stale"))
    idx.upsert("arch1", "lifecycle topic", _meta(lifecycle="archived"))
    results = idx.search("lifecycle topic", "p1", limit=10)
    ids = {r.record_id for r in results}
    assert ids == {"active1"}


def test_lifecycle_filter_explicit_states(tmp_path):
    idx = _index(tmp_path)
    idx.upsert("active1", "lifecycle topic", _meta(lifecycle="active"))
    idx.upsert("stale1", "lifecycle topic", _meta(lifecycle="stale"))
    results = idx.search(
        "lifecycle topic", "p1", limit=10, lifecycle_states=["stale"]
    )
    assert {r.record_id for r in results} == {"stale1"}


def test_persistence_across_reopen(tmp_path):
    db = tmp_path / "vector.db"
    idx = SqliteVecIndex(db, FakeProvider())
    idx.upsert("c1", "persistent vector content", _meta())
    idx.close()

    reopened = SqliteVecIndex(db, FakeProvider())
    results = reopened.search("persistent vector content", "p1", limit=5)
    assert results and results[0].record_id == "c1"


def test_delete_and_stats(tmp_path):
    idx = _index(tmp_path)
    idx.upsert("c1", "alpha", _meta())
    idx.upsert("c2", "beta", _meta(record_type="paragraph"))
    stats = idx.get_stats("p1")
    assert stats["embedded_count"] == 2
    assert stats["by_record_type"]["chunk"] == 1
    assert stats["by_record_type"]["paragraph"] == 1
    assert stats["orphan_count"] == 0

    idx.delete("c1")
    assert idx.get_stats("p1")["embedded_count"] == 1


def test_clear_project(tmp_path):
    idx = _index(tmp_path)
    idx.upsert("c1", "alpha", _meta())
    idx.upsert("c2", "beta", _meta())
    idx.clear_project("p1")
    assert idx.get_stats("p1")["embedded_count"] == 0


def test_record_type_filter(tmp_path):
    idx = _index(tmp_path)
    idx.upsert("c1", "topic match", _meta(record_type="chunk"))
    idx.upsert("p1r", "topic match", _meta(record_type="proposition"))
    results = idx.search("topic match", "p1", limit=10, record_types=["proposition"])
    assert {r.record_id for r in results} == {"p1r"}
