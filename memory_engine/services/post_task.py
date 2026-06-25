"""PostTaskService — orchestrates the full post-task reflection pipeline.

Called once by the agent after completing a task.
No user commands.  No manual promote step.

Flow:
  1. ReflectionSkill.analyze()  → ReflectionAnalysis
  2. If analysis.worth_retaining is False → return early with skip info.
  3. For each suggested_candidate:
       a. CandidateRepository.create()  → staged MemoryCandidateORM
       b. PromotionService.promote()    → PromoteResult
  4. ConsolidationService.update_ancestors() on every new / updated node.
  5. Return PostTaskResult.

All steps run in the same DB session (single transaction per request).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    CandidateCreate,
    PostTaskResult,
    PromoteAction,
    ReflectionAnalysis,
    ReflectionInput,
    ReflectionSkipReason,
)
from memory_engine.repositories.candidate import CandidateRepository
from memory_engine.services.consolidation import ConsolidationService
from memory_engine.services.promotion import PromotionService
from memory_engine.skills.reflection import ReflectionSkill


class PostTaskService:
    """Single entry-point for agent post-task reflection.

    The agent calls `reflect_and_write(ReflectionInput)`.
    The service handles all staging, promotion, and consolidation internally.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._reflection = ReflectionSkill()
        self._candidates = CandidateRepository(session)
        self._promotion = PromotionService(session)
        self._consolidation = ConsolidationService(session)

    # ------------------------------------------------------------------
    # Public API — one method, called by the agent
    # ------------------------------------------------------------------

    def reflect_and_write(self, inp: ReflectionInput) -> PostTaskResult:
        """Run the full reflection + promotion pipeline for a completed task.

        Returns PostTaskResult immediately.  No user interaction required.
        """
        project_id = inp.project_id

        # ── Step 1: Reflection analysis ──────────────────────────────────
        analysis: ReflectionAnalysis = self._reflection.analyze(inp)

        if not analysis.worth_retaining:
            return PostTaskResult(
                project_id=project_id,
                reflection=analysis,
                reflection_skipped=True,
                skip_reason=analysis.skip_reason,
            )

        # ── Step 2: Stage candidates and run promotion pipeline ──────────
        promote_results = []
        consolidation_notes: list[str] = []
        n_promoted = 0
        n_discarded = 0
        n_review = 0

        for candidate_data in analysis.suggested_candidates:
            # a) Persist to staging table
            candidate_orm = self._candidates.create(
                project_id=str(project_id),
                title=candidate_data.title,
                summary=candidate_data.summary,
                proposed_kind=candidate_data.proposed_kind.value,
                proposed_tags=candidate_data.proposed_tags,
                proposed_module_path=candidate_data.proposed_module_path,
                proposed_parent_id=(
                    str(candidate_data.proposed_parent_id)
                    if candidate_data.proposed_parent_id else None
                ),
                source_ref=candidate_data.source_ref,
                confidence=candidate_data.confidence,
                importance=candidate_data.importance,
                evidence_content=candidate_data.evidence_content,
                evidence_source=candidate_data.evidence_source,
            )

            # b) Promote immediately (single pipeline call)
            try:
                result = self._promotion.promote(str(candidate_orm.id))
                promote_results.append(result)
                consolidation_notes.extend(result.consolidation_notes)

                if result.action == PromoteAction.discard:
                    n_discarded += 1
                elif result.action == PromoteAction.needs_review:
                    n_review += 1
                else:
                    n_promoted += 1
                    # c) Consolidate ancestors of newly created / updated nodes
                    if result.target_node is not None:
                        notes = self._consolidation.update_ancestors(
                            str(result.target_node.id)
                        )
                        consolidation_notes.extend(notes)

            except Exception as exc:
                # Never crash the agent — record the failure and continue
                consolidation_notes.append(
                    f"Promotion failed for candidate '{candidate_data.title}': {exc}"
                )

        return PostTaskResult(
            project_id=project_id,
            reflection=analysis,
            promotion_results=promote_results,
            candidates_staged=len(analysis.suggested_candidates),
            candidates_promoted=n_promoted,
            candidates_discarded=n_discarded,
            candidates_needs_review=n_review,
            consolidation_notes=consolidation_notes,
        )
