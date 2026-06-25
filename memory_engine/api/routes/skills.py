"""Agent skill endpoints — designed for agent-to-service use, not manual invocation.

POST /v1/skills/route-memory    — classify task, return RoutingPlan
POST /v1/skills/recall-memory   — autonomous hierarchical recall, return RecallResult
POST /v1/skills/inspect-memory  — progressive drill-down, return InspectResult
"""

from fastapi import APIRouter, Depends, HTTPException

from memory_engine.api.deps import inspect_service, recall_service, skill_router
from memory_engine.models.domain import (
    InspectRequest,
    InspectResult,
    RecallRequest,
    RecallResult,
    RouteRequest,
    RoutingPlan,
)
from memory_engine.services.memory_service import MemoryNodeNotFoundError, ProjectNotFoundError
from memory_engine.skills.inspect import InspectService
from memory_engine.skills.recall import RecallService
from memory_engine.skills.router import SkillRouter

router = APIRouter(prefix="/v1/skills", tags=["skills"])


@router.post("/route-memory", response_model=RoutingPlan)
def route_memory(
    request: RouteRequest,
    router_svc: SkillRouter = Depends(skill_router),
) -> RoutingPlan:
    """Classify the task and return a routing plan.

    The coding agent calls this before deciding whether to recall memory.
    Returns the same plan deterministically for the same input.
    """
    return router_svc.route(request)


@router.post("/recall-memory", response_model=RecallResult)
def recall_memory(
    request: RecallRequest,
    svc: RecallService = Depends(recall_service),
) -> RecallResult:
    """Retrieve persistent memory for the current agent task.

    If no routing_plan is supplied, the endpoint calls the SkillRouter
    internally and routes according to the result.

    Returns an EnrichedContextPack with full retrieval trace.
    """
    try:
        return svc.recall(request)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Project not found: {exc}") from exc


@router.post("/inspect-memory", response_model=InspectResult)
def inspect_memory(
    request: InspectRequest,
    svc: InspectService = Depends(inspect_service),
) -> InspectResult:
    """Drill down into a specific memory node.

    Returns children, related memories, evidence references, conflicts,
    and a confidence assessment.  Used by the agent when high-level recall
    is insufficient for safe implementation.
    """
    try:
        return svc.inspect(request)
    except MemoryNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Memory node not found: {exc}") from exc
