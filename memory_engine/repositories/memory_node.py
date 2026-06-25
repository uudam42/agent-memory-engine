"""MemoryNode repository — raw DB access, no business logic."""

from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from memory_engine.models.orm import MemoryNodeORM


class MemoryNodeRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        project_id: str,
        parent_id: str | None,
        title: str,
        summary: str,
        kind: str,
        depth: int,
        tags: list[str],
        status: str = "active",
        confidence: float = 1.0,
        importance: float = 0.5,
        module_path: str | None = None,
    ) -> MemoryNodeORM:
        obj = MemoryNodeORM(
            project_id=project_id,
            parent_id=parent_id,
            title=title,
            summary=summary,
            kind=kind,
            depth=depth,
            tags=tags,
            status=status,
            confidence=confidence,
            importance=importance,
            module_path=module_path,
        )
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def get(self, node_id: str) -> MemoryNodeORM | None:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(MemoryNodeORM.id == node_id)
            .first()
        )

    def get_bare(self, node_id: str) -> MemoryNodeORM | None:
        """Get node without eagerly loading evidence (for summary updates)."""
        return self._s.get(MemoryNodeORM, node_id)

    def list_by_project(self, project_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(MemoryNodeORM.project_id == project_id)
            .order_by(MemoryNodeORM.depth, MemoryNodeORM.created_at)
            .all()
        )

    def list_active_by_project(self, project_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(
                MemoryNodeORM.project_id == project_id,
                MemoryNodeORM.status == "active",
            )
            .order_by(MemoryNodeORM.depth, MemoryNodeORM.created_at)
            .all()
        )

    def list_by_project_and_statuses(
        self, project_id: str, statuses: list[str]
    ) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(
                MemoryNodeORM.project_id == project_id,
                MemoryNodeORM.status.in_(statuses),
            )
            .order_by(MemoryNodeORM.depth, MemoryNodeORM.created_at)
            .all()
        )

    def list_children(self, parent_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(MemoryNodeORM.parent_id == parent_id)
            .order_by(MemoryNodeORM.created_at)
            .all()
        )

    def list_roots(self, project_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .filter(
                MemoryNodeORM.project_id == project_id,
                MemoryNodeORM.parent_id.is_(None),
            )
            .order_by(MemoryNodeORM.created_at)
            .all()
        )

    # ------------------------------------------------------------------
    # Phase 3 — mutation helpers
    # ------------------------------------------------------------------

    def update_status(self, node_id: str, status: str) -> MemoryNodeORM | None:
        obj = self.get_bare(node_id)
        if obj is None:
            return None
        obj.status = status
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def update_summary(self, node_id: str, summary: str) -> MemoryNodeORM | None:
        """Replace the summary of a node (used by ConsolidationService)."""
        obj = self.get_bare(node_id)
        if obj is None:
            return None
        obj.summary = summary
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def update_fields(
        self,
        node_id: str,
        *,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> MemoryNodeORM | None:
        """Patch a subset of mutable fields."""
        obj = self.get_bare(node_id)
        if obj is None:
            return None
        if summary is not None:
            obj.summary = summary
        if confidence is not None:
            obj.confidence = confidence
        if importance is not None:
            obj.importance = importance
        if tags is not None:
            obj.tags = tags
        if status is not None:
            obj.status = status
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def delete(self, node_id: str) -> bool:
        obj = self._s.get(MemoryNodeORM, node_id)
        if obj is None:
            return False
        self._s.delete(obj)
        self._s.commit()
        return True
