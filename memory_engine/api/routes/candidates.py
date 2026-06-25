"""Candidate staging endpoints.

POST   /projects/{project_id}/candidates
GET    /projects/{project_id}/candidates
GET    /projects/{project_id}/candidates/{candidate_id}
POST   /projects/{project_id}/candidates/{candidate_id}/promote
POST   /memories/{node_id}/stale
GET    /memories/{node_id}/relations
"""

from fastapi import APIRouter, Depends, HTTPException, status

from memory_engine.api.deps import db_session
from memory_engine.models.domain import (
    CandidateCreate,
    MarkStaleRequest,
    MarkStaleResult,
    MemoryNode,
    MemoryRelation,
    MemoryStatus,
    PersistedCandidate,
    PromoteResult,
)
from memory_engine.repositories.candidate import CandidateRepository
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.repositories.relation import RelationRepository
from memory_engine.services.memory_service import MemoryNodeNotFoundError
from memory_engine.services.promotion import (
    CandidateNotFoundError,
    ProjectNotFoundError,
    PromotionService,
)
from sqlalchemy.orm import Session

router = APIRouter(tags=["candidates"])


def _candidate_svc(session: Session = Depends(db_session)) -> PromotionService:
    return PromotionService(session)


def _candidate_repo(session: Session = Depends(db_session)) -> CandidateRepository:
    return CandidateRepository(session)


def _node_repo(session: Session = Depends(db_session)) -> MemoryNodeRepository:
    return MemoryNodeRepository(session)


def _relation_repo(session: Session = Depends(db_session)) -> RelationRepository:
    return RelationRepository(session)


# ---------------------------------------------------------------------------
# Candidate CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/candidates",
    response_model=PersistedCandidate,
    status_code=status.HTTP_201_CREATED,
)
def create_candidate(
    project_id: str,
    payload: CandidateCreate,
    repo: CandidateRepository = Depends(_candidate_repo),
) -> PersistedCandidate:
    """Stage a memory candidate for later promotion."""
    orm = repo.create(
        project_id=project_id,
        title=payload.title,
        summary=payload.summary,
        proposed_kind=payload.proposed_kind.value,
        proposed_tags=payload.proposed_tags,
        proposed_module_path=payload.proposed_module_path,
        proposed_parent_id=(
            str(payload.proposed_parent_id) if payload.proposed_parent_id else None
        ),
        source_ref=payload.source_ref,
        confidence=payload.confidence,
        importance=payload.importance,
        evidence_content=payload.evidence_content,
        evidence_source=payload.evidence_source,
    )
    return PersistedCandidate.model_validate(orm)


@router.get(
    "/projects/{project_id}/candidates",
    response_model=list[PersistedCandidate],
)
def list_candidates(
    project_id: str,
    candidate_status: str | None = None,
    repo: CandidateRepository = Depends(_candidate_repo),
) -> list[PersistedCandidate]:
    """List all candidates for a project, optionally filtered by status."""
    orms = repo.list_by_project(project_id, status=candidate_status)
    return [PersistedCandidate.model_validate(o) for o in orms]


@router.get(
    "/projects/{project_id}/candidates/{candidate_id}",
    response_model=PersistedCandidate,
)
def get_candidate(
    project_id: str,
    candidate_id: str,
    repo: CandidateRepository = Depends(_candidate_repo),
) -> PersistedCandidate:
    orm = repo.get(candidate_id)
    if orm is None or orm.project_id != project_id:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return PersistedCandidate.model_validate(orm)


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/candidates/{candidate_id}/promote",
    response_model=PromoteResult,
)
def promote_candidate(
    project_id: str,
    candidate_id: str,
    svc: PromotionService = Depends(_candidate_svc),
) -> PromoteResult:
    """Run the promotion pipeline for a staged candidate."""
    try:
        return svc.promote(candidate_id)
    except CandidateNotFoundError:
        raise HTTPException(status_code=404, detail="Candidate not found")
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found")


# ---------------------------------------------------------------------------
# Lifecycle — mark stale
# ---------------------------------------------------------------------------


@router.post(
    "/memories/{node_id}/stale",
    response_model=MarkStaleResult,
)
def mark_stale(
    node_id: str,
    payload: MarkStaleRequest,
    svc: PromotionService = Depends(_candidate_svc),
    repo: MemoryNodeRepository = Depends(_node_repo),
) -> MarkStaleResult:
    """Mark a memory node as stale. It remains traceable but excluded from primary recall."""
    existing = repo.get(node_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Memory node not found")

    from memory_engine.models.domain import MemoryStatus as MS
    previous = MS(existing.status)
    try:
        svc.mark_stale(node_id, reason=payload.reason)
    except MemoryNodeNotFoundError:
        raise HTTPException(status_code=404, detail="Memory node not found")

    return MarkStaleResult(
        node_id=existing.id,  # type: ignore[arg-type]
        previous_status=previous,
        new_status=MS.stale,
        reason=payload.reason,
    )


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


@router.get(
    "/memories/{node_id}/relations",
    response_model=list[MemoryRelation],
)
def get_relations(
    node_id: str,
    repo: RelationRepository = Depends(_relation_repo),
) -> list[MemoryRelation]:
    """Return all MemoryRelations where this node is source or target."""
    orms = repo.list_by_node(node_id)
    return [MemoryRelation.model_validate(o) for o in orms]
