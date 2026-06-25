"""Tests for evidence attachment."""

import pytest
from uuid import uuid4

from memory_engine.models.domain import EvidenceCreate, MemoryKind, MemoryNodeCreate
from memory_engine.services.memory_service import MemoryNodeNotFoundError, MemoryService


def test_attach_evidence(memory_service: MemoryService, demo_project):
    node = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="DB Module",
            summary="Database layer",
            kind=MemoryKind.module,
        )
    )

    ev = memory_service.add_evidence(
        EvidenceCreate(
            memory_node_id=node.id,
            content="SQLite is used for local persistence.",
            source="docs/architecture.md",
        )
    )

    assert ev.memory_node_id == node.id
    assert "SQLite" in ev.content
    assert ev.source == "docs/architecture.md"
    assert ev.id is not None


def test_evidence_separate_from_summary(memory_service: MemoryService, demo_project):
    """Evidence must be stored separately — not mixed into the node summary."""
    node = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Config Module",
            summary="Handles app config",
            kind=MemoryKind.module,
        )
    )
    memory_service.add_evidence(
        EvidenceCreate(
            memory_node_id=node.id,
            content="Config is loaded via pydantic-settings",
        )
    )

    fetched_node = memory_service.get_node(str(node.id))
    # Summary is unchanged
    assert fetched_node.summary == "Handles app config"
    # Evidence is in its own list
    assert len(fetched_node.evidence) == 1
    assert "pydantic-settings" in fetched_node.evidence[0].content


def test_multiple_evidence(memory_service: MemoryService, demo_project):
    node = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Multi",
            summary="Multiple evidence items",
            kind=MemoryKind.debug,
        )
    )
    for i in range(3):
        memory_service.add_evidence(
            EvidenceCreate(memory_node_id=node.id, content=f"Evidence {i}")
        )

    evidence_list = memory_service.list_evidence(str(node.id))
    assert len(evidence_list) == 3


def test_evidence_invalid_node_raises(memory_service: MemoryService):
    with pytest.raises(MemoryNodeNotFoundError):
        memory_service.add_evidence(
            EvidenceCreate(
                memory_node_id=uuid4(),
                content="Orphaned evidence",
            )
        )
