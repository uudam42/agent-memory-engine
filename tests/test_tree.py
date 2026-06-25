"""Tests for parent-child tree relationships and rendering."""

import pytest

from memory_engine.config import settings
from memory_engine.models.domain import MemoryKind, MemoryNodeCreate
from memory_engine.services.memory_service import DepthLimitExceededError, MemoryService
from memory_engine.services.tree_service import TreeService
from sqlalchemy.orm import Session


def test_parent_child_depth(memory_service: MemoryService, demo_project):
    root = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Root",
            summary="Root node",
            kind=MemoryKind.architecture,
        )
    )
    assert root.depth == 0

    child = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            parent_id=root.id,
            title="Child",
            summary="Child node",
            kind=MemoryKind.module,
        )
    )
    assert child.depth == 1
    assert child.parent_id == root.id


def test_grandchild_depth(memory_service: MemoryService, demo_project):
    root = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Root",
            summary="Root",
            kind=MemoryKind.architecture,
        )
    )
    child = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            parent_id=root.id,
            title="Child",
            summary="Child",
            kind=MemoryKind.module,
        )
    )
    grandchild = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            parent_id=child.id,
            title="Grandchild",
            summary="Grandchild",
            kind=MemoryKind.procedure,
        )
    )
    assert grandchild.depth == 2


def test_invalid_depth_raises(memory_service: MemoryService, demo_project):
    """Nodes beyond max_tree_depth must be rejected."""
    # Create a chain up to max depth
    parent_id = None
    for _ in range(settings.max_tree_depth + 1):
        node = memory_service.create_node(
            MemoryNodeCreate(
                project_id=demo_project.id,
                parent_id=parent_id,
                title="Node",
                summary="At depth limit",
                kind=MemoryKind.module,
            )
        )
        parent_id = node.id

    # One more must fail
    with pytest.raises(DepthLimitExceededError):
        memory_service.create_node(
            MemoryNodeCreate(
                project_id=demo_project.id,
                parent_id=parent_id,
                title="Too deep",
                summary="Beyond limit",
                kind=MemoryKind.module,
            )
        )


def test_tree_rendering(memory_service: MemoryService, session: Session, demo_project):
    root = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Backend",
            summary="Backend system",
            kind=MemoryKind.architecture,
        )
    )
    memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            parent_id=root.id,
            title="Auth",
            summary="Auth module",
            kind=MemoryKind.module,
        )
    )

    svc = TreeService(session)
    text = svc.render_text(str(demo_project.id))

    assert "Backend" in text
    assert "Auth" in text
    # Child should have deeper indentation (more leading spaces) than root
    lines = text.splitlines()
    root_line = next(l for l in lines if "Backend" in l)
    child_line = next(l for l in lines if "Auth" in l)
    root_indent = len(root_line) - len(root_line.lstrip())
    child_indent = len(child_line) - len(child_line.lstrip())
    assert child_indent > root_indent


def test_tree_struct(memory_service: MemoryService, session: Session, demo_project):
    root = memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            title="Root",
            summary="Root",
            kind=MemoryKind.architecture,
        )
    )
    memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            parent_id=root.id,
            title="Child A",
            summary="A",
            kind=MemoryKind.module,
        )
    )
    memory_service.create_node(
        MemoryNodeCreate(
            project_id=demo_project.id,
            parent_id=root.id,
            title="Child B",
            summary="B",
            kind=MemoryKind.module,
        )
    )

    svc = TreeService(session)
    roots = svc.build_tree(str(demo_project.id))

    assert len(roots) == 1
    assert roots[0].node.title == "Root"
    assert len(roots[0].children) == 2


def test_api_tree(api_client):
    proj_resp = api_client.post("/projects", json={"name": "tree-api-project"})
    project_id = proj_resp.json()["id"]
    api_client.post(
        "/memories",
        json={
            "project_id": project_id,
            "title": "Root node",
            "summary": "Root",
            "kind": "architecture",
        },
    )
    response = api_client.get(f"/projects/{project_id}/tree")
    assert response.status_code == 200
    body = response.json()
    assert "tree" in body
    assert "text" in body
    assert len(body["tree"]) == 1
    assert body["tree"][0]["title"] == "Root node"
