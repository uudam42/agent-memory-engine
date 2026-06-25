"""POST /v1/skills/reflect-and-write — agent post-task reflection endpoint.

The coding agent calls this once after completing a task.
No user commands are needed.  The service handles:
  - knowledge-value assessment
  - candidate generation
  - staging → promotion pipeline
  - parent summary consolidation

Returns PostTaskResult with a full account of what was created, updated,
discarded, or flagged for human review.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from memory_engine.api.deps import db_session
from memory_engine.models.domain import PostTaskResult, ReflectionInput
from memory_engine.services.post_task import PostTaskService

router = APIRouter(prefix="/v1/skills", tags=["reflection"])


def _post_task_service(session: Session = Depends(db_session)) -> PostTaskService:
    return PostTaskService(session)


@router.post("/reflect-and-write", response_model=PostTaskResult)
def reflect_and_write(
    payload: ReflectionInput,
    svc: PostTaskService = Depends(_post_task_service),
) -> PostTaskResult:
    """Run post-task reflection and automatically write memory.

    The coding agent calls this endpoint after completing a task.
    It does NOT require a human to type `memory write` or `memory promote`.

    The system will:
      1. Assess whether the task outcome contains knowledge worth retaining.
      2. Generate zero or more MemoryCandidates (constraint / incident /
         procedure / module / decision).
      3. Run each candidate through the promotion pipeline (dedup → conflict →
         create / merge / update / supersede / discard / needs_review).
      4. Consolidate parent summaries where appropriate.

    If reflection_skipped is True, no candidates were generated (the task
    was trivial, failed, unverified, or produced no new knowledge).
    """
    try:
        return svc.reflect_and_write(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
