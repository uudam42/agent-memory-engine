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

        # Issue 3: callers may set trust_level explicitly (e.g. a caller that
        # knows the content is human-authored policy); otherwise fall back to
        # the same conservative, provenance-based default promotion.py uses.
        trust_level = payload.trust_level
        if trust_level is None:
            from memory_engine.services.source_trust import assign_creation_trust
            trust_level = assign_creation_trust(
                kind=payload.kind, source_path=payload.source_path
            ).value

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
            source_path=payload.source_path,
            source_hash=payload.source_hash,
            source_symbol=payload.source_symbol,
            constraint_scope=payload.constraint_scope,
            constraint_scope_ref=payload.constraint_scope_ref,
            trust_level=trust_level,
            evidence_level=payload.evidence_level,
            verification_evidence=(
                payload.verification_evidence.model_dump(mode="json")
                if payload.verification_evidence is not None
                else None
            ),
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
