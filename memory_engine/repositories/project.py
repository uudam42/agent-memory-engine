"""Project repository — raw DB access, no business logic."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from memory_engine.models.orm import ProjectORM


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(self, *, name: str, description: str | None) -> ProjectORM:
        obj = ProjectORM(name=name, description=description)
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def get(self, project_id: str) -> ProjectORM | None:
        return self._s.get(ProjectORM, project_id)

    def get_by_name(self, name: str) -> ProjectORM | None:
        return self._s.query(ProjectORM).filter(ProjectORM.name == name).first()

    def list_all(self) -> list[ProjectORM]:
        return self._s.query(ProjectORM).order_by(ProjectORM.created_at).all()

    def delete(self, project_id: str) -> bool:
        obj = self.get(project_id)
        if obj is None:
            return False
        self._s.delete(obj)
        self._s.commit()
        return True
