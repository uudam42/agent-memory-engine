"""MemoryNode and Evidence service — business logic layer."""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.config import settings
from memory_engine.models.domain import Evidence, EvidenceCreate, MemoryNode, MemoryNodeCreate
from memory_engine.repositories.evidence import EvidenceRepository
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.repositories.project import ProjectRepository


class MemoryNodeNotFoundError(KeyError):
    pass


class ProjectNotFoundError(KeyError):
    pass


class DepthLimitExceededError(ValueError):
    pass


class MemoryService:
    def __init__(self, session: Session) -> None:
        self._nodes = MemoryNodeRepository(session)
        self._evidence = EvidenceRepository(session)
        self._projects = ProjectRepository(session)

    # ------------------------------------------------------------------
    # MemoryNode
    # ------------------------------------------------------------------

    def create_node(self, payload: MemoryNodeCreate) -> MemoryNode:
        project = self._projects.get(str(payload.project_id))
        if project is None:
            raise ProjectNotFoundError(str(payload.project_id))

        depth = 0
        if payload.parent_id is not None:
            parent = self._nodes.get(str(payload.parent_id))
            if parent is None:
                raise MemoryNodeNotFoundError(str(payload.parent_id))
            depth = parent.depth + 1

        if depth > settings.max_tree_depth:
            raise DepthLimitExceededError(
                f"Tree depth {depth} exceeds maximum allowed depth {settings.max_tree_depth}."
            )

        orm = self._nodes.create(
            project_id=str(payload.project_id),
            parent_id=str(payload.parent_id) if payload.parent_id else None,
            title=payload.title,
            summary=payload.summary,
            kind=payload.kind.value,
            depth=depth,
            tags=payload.tags,
            # Stage 2 fields
            status=payload.status.value,
            confidence=payload.confidence,
            importance=payload.importance,
            module_path=payload.module_path,
        )
        return MemoryNode.model_validate(orm)

    def get_node(self, node_id: str) -> MemoryNode:
        orm = self._nodes.get(node_id)
        if orm is None:
            raise MemoryNodeNotFoundError(node_id)
        return MemoryNode.model_validate(orm)

    def list_by_project(self, project_id: str) -> list[MemoryNode]:
        return [MemoryNode.model_validate(o) for o in self._nodes.list_by_project(project_id)]

    def delete_node(self, node_id: str) -> None:
        if not self._nodes.delete(node_id):
            raise MemoryNodeNotFoundError(node_id)

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    def add_evidence(self, payload: EvidenceCreate) -> Evidence:
        node = self._nodes.get(str(payload.memory_node_id))
        if node is None:
            raise MemoryNodeNotFoundError(str(payload.memory_node_id))

        orm = self._evidence.create(
            memory_node_id=str(payload.memory_node_id),
            content=payload.content,
            source=payload.source,
        )
        return Evidence.model_validate(orm)

    def list_evidence(self, node_id: str) -> list[Evidence]:
        return [
            Evidence.model_validate(e)
            for e in self._evidence.list_by_node(node_id)
        ]

    def delete_evidence(self, evidence_id: str) -> None:
        if not self._evidence.delete(evidence_id):
            raise KeyError(evidence_id)
