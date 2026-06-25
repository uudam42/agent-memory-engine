"""Tests for InspectService — progressive drill-down."""

import pytest

from memory_engine.models.domain import (
    InspectRequest,
    MemoryKind,
    MemoryNodeCreate,
    MemoryStatus,
    EvidenceCreate,
    MemoryRelationCreate,
    RelationType,
)
from memory_engine.services.memory_service import MemoryService, MemoryNodeNotFoundError
from memory_engine.skills.inspect import InspectService
from uuid import uuid4


def _inspect_req(project_id, memory_id: str, **kwargs) -> InspectRequest:
    return InspectRequest(project_id=project_id, memory_id=memory_id, **kwargs)


# ---------------------------------------------------------------------------
# 1. Basic inspection returns node + confidence assessment
# ---------------------------------------------------------------------------


def test_inspect_returns_target_node(inspect_service: InspectService, scheduler_demo):
    lifecycle = scheduler_demo["lifecycle"]
    result = inspect_service.inspect(_inspect_req(
        scheduler_demo["project"].id,
        str(lifecycle.id),
    ))
    assert result.memory.title == lifecycle.title
    assert result.confidence_assessment.confidence >= 0.0
    assert result.confidence_assessment.freshness > 0.0


# ---------------------------------------------------------------------------
# 2. Inspection returns children
# ---------------------------------------------------------------------------


def test_inspect_returns_children(inspect_service: InspectService, scheduler_demo):
    arch = scheduler_demo["arch"]
    result = inspect_service.inspect(_inspect_req(
        scheduler_demo["project"].id,
        str(arch.id),
        inspection_depth=1,
    ))
    # arch has 3 children in the demo: lifecycle, retry_policy, runtime
    assert len(result.children) == 3


# ---------------------------------------------------------------------------
# 3. Progressive drill-down — depth=2 includes grandchildren
# ---------------------------------------------------------------------------


def test_inspect_progressive_drill_down(
    inspect_service: InspectService, memory_service: MemoryService, demo_project
):
    root = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, title="Root", summary="Root node",
        kind=MemoryKind.architecture,
    ))
    child = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, parent_id=root.id,
        title="Child", summary="Child node", kind=MemoryKind.module,
    ))
    grandchild = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, parent_id=child.id,
        title="Grandchild", summary="Deep node", kind=MemoryKind.procedure,
    ))

    result = inspect_service.inspect(_inspect_req(
        demo_project.id, str(root.id), inspection_depth=2
    ))
    child_titles = {c.title for c in result.children}
    assert "Child" in child_titles
    assert "Grandchild" in child_titles


def test_inspect_depth_1_excludes_grandchildren(
    inspect_service: InspectService, memory_service: MemoryService, demo_project
):
    root = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, title="Root2", summary="Root",
        kind=MemoryKind.architecture,
    ))
    child = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, parent_id=root.id,
        title="Child2", summary="Child", kind=MemoryKind.module,
    ))
    grandchild = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, parent_id=child.id,
        title="Grandchild2", summary="Deep", kind=MemoryKind.procedure,
    ))

    result = inspect_service.inspect(_inspect_req(
        demo_project.id, str(root.id), inspection_depth=1
    ))
    child_titles = {c.title for c in result.children}
    assert "Child2" in child_titles
    assert "Grandchild2" not in child_titles


# ---------------------------------------------------------------------------
# 4. Related memories via MemoryRelation
# ---------------------------------------------------------------------------


def test_inspect_returns_related_memories(inspect_service: InspectService, scheduler_demo):
    lifecycle = scheduler_demo["lifecycle"]
    result = inspect_service.inspect(_inspect_req(
        scheduler_demo["project"].id, str(lifecycle.id),
    ))
    # lifecycle has a relation to terminal_decision (depends_on)
    # incident has a relation to lifecycle (related_to)
    related_titles = {m.title for m in result.related_memories}
    conflict_titles = {m.title for m in result.conflicts}
    all_peer_titles = related_titles | conflict_titles
    # At least one related memory should be present
    assert len(all_peer_titles) >= 1


# ---------------------------------------------------------------------------
# 5. Stale/superseded peers detected as conflicts
# ---------------------------------------------------------------------------


def test_inspect_detects_stale_related_memory(
    inspect_service: InspectService,
    memory_service: MemoryService,
    demo_project,
    session,
):
    from memory_engine.repositories.relation import RelationRepository

    node_a = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, title="Active Node", summary="Current design",
        kind=MemoryKind.module,
    ))
    node_b = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, title="Stale Node", summary="Old design",
        kind=MemoryKind.module, status=MemoryStatus.stale,
    ))

    r_repo = RelationRepository(session)
    r_repo.create(
        source_id=str(node_a.id),
        target_id=str(node_b.id),
        relation_type=RelationType.related_to.value,
    )

    result = inspect_service.inspect(_inspect_req(demo_project.id, str(node_a.id)))
    conflict_titles = {c.title for c in result.conflicts}
    assert "Stale Node" in conflict_titles
    assert any("stale" in t.lower() for t in result.inspection_trace)


# ---------------------------------------------------------------------------
# 6. Evidence is included when requested
# ---------------------------------------------------------------------------


def test_inspect_includes_evidence(inspect_service: InspectService, scheduler_demo):
    incident = scheduler_demo["incident"]
    result = inspect_service.inspect(_inspect_req(
        scheduler_demo["project"].id, str(incident.id), include_evidence=True
    ))
    assert len(result.evidence_refs) > 0
    assert "PR #142" in result.evidence_refs[0].content


def test_inspect_excludes_evidence_when_not_requested(
    inspect_service: InspectService, scheduler_demo
):
    incident = scheduler_demo["incident"]
    result = inspect_service.inspect(_inspect_req(
        scheduler_demo["project"].id, str(incident.id), include_evidence=False
    ))
    assert result.evidence_refs == []


# ---------------------------------------------------------------------------
# 7. Unknown node raises error
# ---------------------------------------------------------------------------


def test_inspect_unknown_node_raises(inspect_service: InspectService, demo_project):
    with pytest.raises(MemoryNodeNotFoundError):
        inspect_service.inspect(_inspect_req(demo_project.id, str(uuid4())))


# ---------------------------------------------------------------------------
# 8. Inspection trace always populated
# ---------------------------------------------------------------------------


def test_inspection_trace_populated(inspect_service: InspectService, scheduler_demo):
    lifecycle = scheduler_demo["lifecycle"]
    result = inspect_service.inspect(_inspect_req(
        scheduler_demo["project"].id, str(lifecycle.id)
    ))
    assert len(result.inspection_trace) > 0
    assert any("Inspecting" in t for t in result.inspection_trace)


# ---------------------------------------------------------------------------
# 9. Stale node triggers warning in trace
# ---------------------------------------------------------------------------


def test_stale_node_warning_in_trace(
    inspect_service: InspectService, memory_service: MemoryService, demo_project
):
    stale = memory_service.create_node(MemoryNodeCreate(
        project_id=demo_project.id, title="Old approach", summary="Legacy",
        kind=MemoryKind.module, status=MemoryStatus.stale,
    ))
    result = inspect_service.inspect(_inspect_req(demo_project.id, str(stale.id)))
    assert any("stale" in t.lower() for t in result.inspection_trace)
    assert result.confidence_assessment.status == MemoryStatus.stale


# ---------------------------------------------------------------------------
# 10. API endpoint for inspect
# ---------------------------------------------------------------------------


def test_api_inspect_endpoint(api_client, scheduler_demo):
    lifecycle = scheduler_demo["lifecycle"]
    response = api_client.post(
        "/v1/skills/inspect-memory",
        json={
            "project_id": str(scheduler_demo["project"].id),
            "memory_id": str(lifecycle.id),
            "inspection_depth": 1,
            "include_evidence": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "memory" in body
    assert "children" in body
    assert "confidence_assessment" in body
    assert body["memory"]["title"] == lifecycle.title
