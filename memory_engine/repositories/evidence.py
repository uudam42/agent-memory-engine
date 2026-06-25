"""Evidence repository."""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.orm import EvidenceORM


class EvidenceRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        memory_node_id: str,
        content: str,
        source: str | None,
    ) -> EvidenceORM:
        obj = EvidenceORM(memory_node_id=memory_node_id, content=content, source=source)
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def get(self, evidence_id: str) -> EvidenceORM | None:
        return self._s.get(EvidenceORM, evidence_id)

    def list_by_node(self, memory_node_id: str) -> list[EvidenceORM]:
        return (
            self._s.query(EvidenceORM)
            .filter(EvidenceORM.memory_node_id == memory_node_id)
            .order_by(EvidenceORM.created_at)
            .all()
        )

    def delete(self, evidence_id: str) -> bool:
        obj = self.get(evidence_id)
        if obj is None:
            return False
        self._s.delete(obj)
        self._s.commit()
        return True
