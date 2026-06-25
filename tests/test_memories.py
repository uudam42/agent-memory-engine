"""Tests for memory node creation."""

import pytest

from memory_engine.models.domain import MemoryKind, MemoryNodeCreate
from memory_engine.services.memory_service import MemoryService, ProjectNotFoundError
from uuid import uuid4


def test_create_memory_node(memory_service: MemoryService, demo_project):
    node = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Auth Module",
            summary="Handles JWT authentication",
            kind=MemoryKind.module,
            tags=["auth", "jwt"],
        )
    )
    assert node.title == "Auth Module"
    assert node.kind == MemoryKind.module
    assert node.depth == 0
    assert node.tags == ["auth", "jwt"]


def test_create_node_invalid_project(memory_service: MemoryService):
    with pytest.raises(ProjectNotFoundError):
        memory_service.create_node(
            MemoryNodeCreate(
                project_id=uuid4(),
                title="Orphan",
                summary="Has no project",
                kind=MemoryKind.module,
            )
        )


def test_list_nodes_by_project(memory_service: MemoryService, demo_project):
    for i in range(3):
        memory_service.create_node(
            MemoryNodeCreate(
                project_id=demo_project.id,
                title=f"Node {i}",
                summary=f"Summary {i}",
                kind=MemoryKind.architecture,
            )
        )
    nodes = memory_service.list_by_project(str(demo_project.id))
    assert len(nodes) == 3


def test_api_create_memory(api_client):
    # Create project via API first
    proj_resp = api_client.post("/projects", json={"name": "mem-test-project"})
    assert proj_resp.status_code == 201
    project_id = proj_resp.json()["id"]

    response = api_client.post(
        "/memories",
        json={
            "project_id": project_id,
            "title": "API node",
            "summary": "Created via API",
            "kind": "decision",
            "tags": ["api"],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "API node"
    assert body["depth"] == 0
