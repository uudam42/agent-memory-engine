"""CandidateRepository — CRUD for the memory_candidates staging table."""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.orm import MemoryCandidateORM


class CandidateRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        project_id: str,
        title: str,
        summary: str,
        proposed_kind: str,
        proposed_tags: list[str],
        proposed_module_path: str | None,
        proposed_parent_id: str | None,
        source_ref: str | None,
        confidence: float,
        importance: float,
        evidence_content: str | None,
        evidence_source: str | None,
    ) -> MemoryCandidateORM:
        obj = MemoryCandidateORM(
            project_id=project_id,
            title=title,
            summary=summary,
            proposed_kind=proposed_kind,
            proposed_tags=proposed_tags,
            proposed_module_path=proposed_module_path,
            proposed_parent_id=proposed_parent_id,
            source_ref=source_ref,
            confidence=confidence,
            importance=importance,
            evidence_content=evidence_content,
            evidence_source=evidence_source,
            status="pending",
        )
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def get(self, candidate_id: str) -> MemoryCandidateORM | None:
        return self._s.get(MemoryCandidateORM, candidate_id)

    def list_by_project(
        self,
        project_id: str,
        status: str | None = None,
    ) -> list[MemoryCandidateORM]:
        q = self._s.query(MemoryCandidateORM).filter(
            MemoryCandidateORM.project_id == project_id
        )
        if status:
            q = q.filter(MemoryCandidateORM.status == status)
        return q.order_by(MemoryCandidateORM.created_at).all()

    def update_outcome(
        self,
        candidate_id: str,
        *,
        status: str,
        promote_action: str | None,
        target_node_id: str | None,
    ) -> MemoryCandidateORM | None:
        obj = self.get(candidate_id)
        if obj is None:
            return None
        obj.status = status
        obj.promote_action = promote_action
        obj.target_node_id = target_node_id
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def delete(self, candidate_id: str) -> bool:
        obj = self.get(candidate_id)
        if obj is None:
            return False
        self._s.delete(obj)
        self._s.commit()
        return True
