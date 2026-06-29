"""SqliteVecIndex — persistent local vector index using sqlite-vec (Phase 13).

Storage layout (in <project>/.memory-engine/vector.db):

  vec_metadata     — regular SQLite table; one row per embedded record.
                     Tracks project_id, record_type, branch_scope,
                     lifecycle_state, content_hash, model, dimension and the
                     vec_items rowid mapping.
  vec_items        — sqlite-vec virtual table holding the float[dim] vectors,
                     keyed by an integer rowid that mirrors vec_metadata.rowid.

Design guarantees:
  - Idempotent upsert: identical content_hash + model → no re-embed (cache hit).
  - Lifecycle filtering: archived / superseded / stale excluded by default.
  - Branch safety: branch_scope / branch_name preserved so callers can apply the
    same filters semantic results must respect.
  - Project isolation: every query is scoped to a single project_id.
  - Graceful degradation: if sqlite_vec is not importable, instantiation raises
    SqliteVecUnavailable — callers fall back to lexical retrieval.

Vectors are expected to be L2-normalized by the embedding provider; cosine
similarity is therefore computed as 1 - (distance^2 / 2) from the L2 distance
returned by sqlite-vec, clamped to [0, 1].
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory_engine.knowledge.embedding import EmbeddingProvider

# Lifecycle states that are hidden from semantic results by default.
_EXCLUDED_LIFECYCLE = frozenset({"archived", "superseded", "stale", "outdated"})


class SqliteVecUnavailable(RuntimeError):
    """Raised when the sqlite-vec extension cannot be loaded."""


def sqlite_vec_available() -> bool:
    """Return True if the sqlite_vec python package is importable."""
    try:
        import sqlite_vec  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


@dataclass
class VectorSearchResult:
    record_id: str
    score: float                       # cosine similarity in [0, 1]
    metadata: dict[str, Any] = field(default_factory=dict)
    record_type: str = "chunk"
    branch_scope: str | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class SqliteVecIndex:
    """Persistent local vector index using sqlite-vec."""

    def __init__(self, db_path: Path, provider: EmbeddingProvider) -> None:
        if not sqlite_vec_available():
            raise SqliteVecUnavailable(
                "sqlite-vec is not installed. Install with: "
                "uv pip install 'memory-engine[semantic-sqlite]'"
            )
        self._db_path = Path(db_path)
        self._provider = provider
        self._conn = None
        self._dimension = int(getattr(provider, "dimension", 0) or 0)
        self._init_tables()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _get_conn(self):  # type: ignore[no-untyped-def]
        if self._conn is None:
            import sqlite3

            import sqlite_vec  # type: ignore

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path))
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._conn = conn
        return self._conn

    def _ensure_dimension(self) -> int:
        """Resolve the embedding dimension, probing the provider if needed."""
        if self._dimension > 0:
            return self._dimension
        dim = int(getattr(self._provider, "dimension", 0) or 0)
        if dim == 0 and self._provider.is_available():
            probe = self._provider.embed_query("dimension probe")
            dim = len(probe)
        self._dimension = dim
        return dim

    def _init_tables(self) -> None:
        conn = self._get_conn()
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
        dim = self._ensure_dimension()
        if dim > 0:
            self._ensure_vec_items_table(dim)
        conn.commit()

    def _ensure_vec_items_table(self, dim: int) -> None:
        conn = self._get_conn()
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_items
            USING vec0(embedding float[{dim}])
            """
        )

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def upsert(self, record_id: str, content: str, metadata: dict[str, Any]) -> bool:
        """Embed and store a record. Returns True if an embedding was generated.

        Idempotent: if a row with the same record_id, content_hash and model
        already exists, no embedding is generated and False is returned.
        """
        conn = self._get_conn()
        content_hash = _sha256(content)
        model = getattr(self._provider, "model_name", "none")

        row = conn.execute(
            "SELECT rowid, content_hash, model FROM vec_metadata WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is not None and row[1] == content_hash and row[2] == model:
            return False  # cache hit — unchanged

        if not self._provider.is_available():
            return False

        vec = self._provider.embed_query(content)
        if not vec:
            return False

        dim = len(vec)
        if self._dimension == 0:
            self._dimension = dim
        self._ensure_vec_items_table(self._dimension)

        import sqlite_vec  # type: ignore

        packed = sqlite_vec.serialize_float32(vec)

        if row is not None:
            existing_rowid = int(row[0])
            conn.execute(
                "DELETE FROM vec_items WHERE rowid = ?", (existing_rowid,)
            )
            conn.execute(
                """
                UPDATE vec_metadata
                SET project_id=?, record_type=?, branch_name=?, branch_scope=?,
                    lifecycle_state=?, content_hash=?, model=?, dimension=?,
                    source_path=?
                WHERE rowid=?
                """,
                (
                    metadata.get("project_id", ""),
                    metadata.get("record_type", "chunk"),
                    metadata.get("branch_name"),
                    metadata.get("branch_scope"),
                    metadata.get("lifecycle_state", "active"),
                    content_hash,
                    model,
                    self._dimension,
                    metadata.get("source_path"),
                    existing_rowid,
                ),
            )
            conn.execute(
                "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
                (existing_rowid, packed),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO vec_metadata
                    (record_id, project_id, record_type, branch_name, branch_scope,
                     lifecycle_state, content_hash, model, dimension, source_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    metadata.get("project_id", ""),
                    metadata.get("record_type", "chunk"),
                    metadata.get("branch_name"),
                    metadata.get("branch_scope"),
                    metadata.get("lifecycle_state", "active"),
                    content_hash,
                    model,
                    self._dimension,
                    metadata.get("source_path"),
                ),
            )
            new_rowid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
                (new_rowid, packed),
            )
        conn.commit()
        return True

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        project_id: str,
        limit: int = 20,
        lifecycle_states: list[str] | None = None,
        branch_name: str | None = None,
        record_types: list[str] | None = None,
    ) -> list[VectorSearchResult]:
        """Return semantically similar records, respecting lifecycle/branch filters."""
        if not query.strip() or not self._provider.is_available():
            return []
        q_vec = self._provider.embed_query(query)
        if not q_vec:
            return []

        conn = self._get_conn()
        # vec_items may not exist yet if nothing was ever embedded.
        try:
            import sqlite_vec  # type: ignore

            packed = sqlite_vec.serialize_float32(q_vec)
            # Over-fetch then filter in Python so metadata filters don't starve
            # the KNN candidate set.
            knn = conn.execute(
                """
                SELECT rowid, distance
                FROM vec_items
                WHERE embedding MATCH ? AND k = ?
                ORDER BY distance
                """,
                (packed, max(limit * 5, limit)),
            ).fetchall()
        except Exception:
            return []

        if not knn:
            return []

        rowids = [int(r[0]) for r in knn]
        dist_map = {int(r[0]): float(r[1]) for r in knn}
        placeholders = ",".join("?" for _ in rowids)
        meta_rows = conn.execute(
            f"""
            SELECT rowid, record_id, project_id, record_type, branch_name,
                   branch_scope, lifecycle_state, source_path
            FROM vec_metadata
            WHERE rowid IN ({placeholders})
            """,
            rowids,
        ).fetchall()

        excluded = _EXCLUDED_LIFECYCLE
        allowed_states = set(lifecycle_states) if lifecycle_states else None
        allowed_types = set(record_types) if record_types else None

        results: list[VectorSearchResult] = []
        for mr in meta_rows:
            (rowid, record_id, p_id, rtype, b_name, b_scope, lifecycle, src_path) = mr
            if p_id != project_id:
                continue
            if allowed_states is not None:
                if lifecycle not in allowed_states:
                    continue
            elif lifecycle in excluded:
                continue
            if allowed_types is not None and rtype not in allowed_types:
                continue
            if branch_name is not None and b_name not in (None, branch_name) \
                    and (b_scope not in (None, "global", "mainline")):
                continue

            dist = dist_map.get(int(rowid), 2.0)
            # L2 distance of unit vectors → cosine similarity.
            score = max(0.0, min(1.0, 1.0 - (dist * dist) / 2.0))
            results.append(
                VectorSearchResult(
                    record_id=record_id,
                    score=score,
                    record_type=rtype,
                    branch_scope=b_scope,
                    metadata={
                        "project_id": p_id,
                        "record_type": rtype,
                        "branch_name": b_name,
                        "branch_scope": b_scope,
                        "lifecycle_state": lifecycle,
                        "source_path": src_path,
                    },
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def delete(self, record_id: str) -> None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT rowid FROM vec_metadata WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return
        rowid = int(row[0])
        try:
            conn.execute("DELETE FROM vec_items WHERE rowid = ?", (rowid,))
        except Exception:
            pass
        conn.execute("DELETE FROM vec_metadata WHERE rowid = ?", (rowid,))
        conn.commit()

    def set_lifecycle(self, record_id: str, lifecycle_state: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE vec_metadata SET lifecycle_state = ? WHERE record_id = ?",
            (lifecycle_state, record_id),
        )
        conn.commit()

    def clear_project(self, project_id: str) -> None:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT rowid FROM vec_metadata WHERE project_id = ?", (project_id,)
        ).fetchall()
        for (rowid,) in rows:
            try:
                conn.execute("DELETE FROM vec_items WHERE rowid = ?", (int(rowid),))
            except Exception:
                pass
        conn.execute("DELETE FROM vec_metadata WHERE project_id = ?", (project_id,))
        conn.commit()

    def has_embedding(self, record_id: str, content: str) -> bool:
        """Return True if record_id is already embedded for this content+model."""
        conn = self._get_conn()
        content_hash = _sha256(content)
        model = getattr(self._provider, "model_name", "none")
        row = conn.execute(
            "SELECT content_hash, model FROM vec_metadata WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        return row is not None and row[0] == content_hash and row[1] == model

    def get_stats(self, project_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM vec_metadata WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        by_type_rows = conn.execute(
            "SELECT record_type, COUNT(*) FROM vec_metadata "
            "WHERE project_id = ? GROUP BY record_type",
            (project_id,),
        ).fetchall()
        by_lifecycle_rows = conn.execute(
            "SELECT lifecycle_state, COUNT(*) FROM vec_metadata "
            "WHERE project_id = ? GROUP BY lifecycle_state",
            (project_id,),
        ).fetchall()
        models = conn.execute(
            "SELECT DISTINCT model FROM vec_metadata WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        # Orphan detection: vec_metadata rows whose vec_items vector is missing.
        orphans = 0
        try:
            orphans = conn.execute(
                """
                SELECT COUNT(*) FROM vec_metadata m
                WHERE m.project_id = ?
                  AND NOT EXISTS (SELECT 1 FROM vec_items v WHERE v.rowid = m.rowid)
                """,
                (project_id,),
            ).fetchone()[0]
        except Exception:
            orphans = 0
        return {
            "embedded_count": int(total),
            "by_record_type": {r[0]: int(r[1]) for r in by_type_rows},
            "by_lifecycle": {r[0]: int(r[1]) for r in by_lifecycle_rows},
            "models": [m[0] for m in models],
            "dimension": self._dimension,
            "orphan_count": int(orphans),
        }

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
