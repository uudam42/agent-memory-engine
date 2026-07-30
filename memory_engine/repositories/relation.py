"""MemoryRelation repository."""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.orm import MemoryRelationORM


class RelationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> MemoryRelationORM:
        obj = MemoryRelationORM(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
        )
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def list_by_source(self, source_id: str) -> list[MemoryRelationORM]:
        return (
            self._s.query(MemoryRelationORM)
            .filter(MemoryRelationORM.source_id == source_id)
            .all()
        )

    def list_by_target(self, target_id: str) -> list[MemoryRelationORM]:
        return (
            self._s.query(MemoryRelationORM)
            .filter(MemoryRelationORM.target_id == target_id)
            .all()
        )

    def list_by_node(self, node_id: str) -> list[MemoryRelationORM]:
        """Return all relations where this node is source or target."""
        return (
            self._s.query(MemoryRelationORM)
            .filter(
                (MemoryRelationORM.source_id == node_id)
                | (MemoryRelationORM.target_id == node_id)
            )
            .all()
        )

    def list_by_node_ids(self, node_ids: list[str]) -> list[MemoryRelationORM]:
        """Relations where BOTH endpoints are in ``node_ids``.

        Bounded lookup for retrieval-time conflict detection (Issue 5):
        ``node_ids`` is expected to be the already-small set of candidates
        that passed every prior recall gate, so this is a single query
        with two ``IN`` clauses — never a full-table scan and never O(n^2)
        over project memory.
        """
        if not node_ids:
            return []
        return (
            self._s.query(MemoryRelationORM)
            .filter(
                MemoryRelationORM.source_id.in_(node_ids),
                MemoryRelationORM.target_id.in_(node_ids),
            )
            .all()
        )
