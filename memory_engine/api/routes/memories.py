"""Memory node endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status

from memory_engine.api.deps import memory_service
from memory_engine.models.domain import Evidence, EvidenceCreate, MemoryNode, MemoryNodeCreate
from memory_engine.services.memory_service import (
    DepthLimitExceededError,
    MemoryNodeNotFoundError,
    MemoryService,
    ProjectNotFoundError,
)

router = APIRouter(prefix="/memories", tags=["memories"])


@router.post("", response_model=MemoryNode, status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: MemoryNodeCreate,
    svc: MemoryService = Depends(memory_service),
) -> MemoryNode:
    try:
        return svc.create_node(payload)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {exc}") from exc
    except MemoryNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Parent node not found: {exc}") from exc
    except DepthLimitExceededError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{node_id}", response_model=MemoryNode)
def get_memory(
    node_id: str,
    svc: MemoryService = Depends(memory_service),
) -> MemoryNode:
    try:
        return svc.get_node(node_id)
    except MemoryNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{node_id}/evidence", response_model=Evidence, status_code=status.HTTP_201_CREATED)
def add_evidence(
    node_id: str,
    payload: EvidenceCreate,
    svc: MemoryService = Depends(memory_service),
) -> Evidence:
    if str(payload.memory_node_id) != node_id:
        raise HTTPException(status_code=422, detail="memory_node_id must match URL node_id")
    try:
        return svc.add_evidence(payload)
    except MemoryNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
