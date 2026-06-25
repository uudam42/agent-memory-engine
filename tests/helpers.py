"""Shared test helpers for Phase 3 tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from memory_engine.models.domain import MemoryKind, MemoryNode, MemoryStatus, PersistedCandidate

_PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def make_project_uuid() -> uuid.UUID:
    return _PROJECT_UUID


def make_candidate(
    *,
    project_id: uuid.UUID | None = None,
    title: str,
    kind: MemoryKind,
    summary: str = "A summary",
    module_path: str | None = None,
    proposed_parent_id: uuid.UUID | None = None,
    confidence: float = 0.9,
    importance: float = 0.5,
) -> PersistedCandidate:
    return PersistedCandidate(
        id=uuid.uuid4(),
        project_id=project_id or _PROJECT_UUID,
        title=title,
        summary=summary,
        proposed_kind=kind,
        proposed_tags=[],
        proposed_module_path=module_path,
        proposed_parent_id=proposed_parent_id,
        source_ref=None,
        confidence=confidence,
        importance=importance,
        evidence_content=None,
        evidence_source=None,
        status="pending",
        promote_action=None,
        target_node_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def make_node(
    *,
    title: str,
    kind: str,
    depth: int = 0,
    module_path: str | None = None,
    confidence: float = 0.9,
    status: str = "active",
    summary: str = "Default summary.",
) -> MemoryNode:
    return MemoryNode(
        id=uuid.uuid4(),
        project_id=_PROJECT_UUID,
        parent_id=None,
        title=title,
        summary=summary,
        kind=kind,
        depth=depth,
        tags=[],
        status=MemoryStatus(status),
        confidence=confidence,
        importance=0.5,
        module_path=module_path,
        created_at=_NOW,
        updated_at=_NOW,
        evidence=[],
    )
