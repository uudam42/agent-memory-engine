"""POST /v1/skills/retrieve-agent-context — unified memory + knowledge retrieval."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from memory_engine.api.deps import db_session
from memory_engine.knowledge.fusion import UnifiedContextRetrievalService
from memory_engine.knowledge.ingestion import get_shared_vector_index
from memory_engine.models.knowledge_domain import (
    UnifiedContextPack,
    UnifiedRetrievalRequest,
)

router = APIRouter(prefix="/v1/skills", tags=["unified-retrieval"])


@router.post("/retrieve-agent-context", response_model=UnifiedContextPack)
def retrieve_agent_context(
    payload: UnifiedRetrievalRequest,
    session: Session = Depends(db_session),
) -> UnifiedContextPack:
    """Primary agent context retrieval endpoint (Phase 6).

    Combines:
      - Hierarchical memory recall (constraints, architecture, incidents,
        decisions, procedures, module summaries)
      - Hybrid knowledge search (FTS5 + vector, fused with RRF)
      - Token-budget enforcement (memory 60%, knowledge 40% by default)
      - Result deduplication
      - Full unified retrieval trace

    The agent calls this once per task.  No manual recall or search commands
    needed.  Returns a UnifiedContextPack.
    """
    svc = UnifiedContextRetrievalService(
        session=session,
        vector_index=get_shared_vector_index(),
    )
    try:
        return svc.retrieve(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
