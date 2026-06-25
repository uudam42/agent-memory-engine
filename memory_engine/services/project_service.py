"""Project service — business logic layer."""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.domain import Project, ProjectCreate
from memory_engine.repositories.project import ProjectRepository


class ProjectAlreadyExistsError(ValueError):
    pass


class ProjectNotFoundError(KeyError):
    pass


def _orm_to_domain(orm: object) -> Project:
    return Project.model_validate(orm)


class ProjectService:
    def __init__(self, session: Session) -> None:
        self._repo = ProjectRepository(session)

    def create(self, payload: ProjectCreate) -> Project:
        existing = self._repo.get_by_name(payload.name)
        if existing is not None:
            raise ProjectAlreadyExistsError(f"Project '{payload.name}' already exists.")
        orm = self._repo.create(name=payload.name, description=payload.description)
        return _orm_to_domain(orm)

    def get(self, project_id: str) -> Project:
        orm = self._repo.get(project_id)
        if orm is None:
            raise ProjectNotFoundError(project_id)
        return _orm_to_domain(orm)

    def list_all(self) -> list[Project]:
        return [_orm_to_domain(o) for o in self._repo.list_all()]

    def delete(self, project_id: str) -> None:
        if not self._repo.delete(project_id):
            raise ProjectNotFoundError(project_id)
