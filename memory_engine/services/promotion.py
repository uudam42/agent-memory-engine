"""PromotionService — orchestrates the full candidate promotion pipeline.

Pipeline for each candidate:

  1. Load candidate from staging table.
  2. PlacementService → decide depth + parent.
  3. DeduplicationService → find duplicate matches.
  4. Branch on duplicate similarity:

     No duplicate (< 0.5):
       → CREATE new node at decided placement.
       → Attach inline evidence if present.
       → Consolidate parent summary.
       → action = create

     Near-duplicate (≥ 0.8), same kind:
       ConflictService.evaluate():
         conflict = confidence_too_low  → action = needs_review
         conflict = content_contradiction → action = needs_review
         no conflict AND candidate.confidence >= existing.confidence:
           → action = supersede
           → mark existing as superseded
           → create new active node
           → create MemoryRelation(new → old, supersedes)
         no conflict AND candidate.confidence < existing.confidence:
           → action = discard (existing is already better)

     Partial match (0.5–0.8):
       ConflictService.evaluate():
         any conflict → action = needs_review
         no conflict AND candidate.confidence >= existing.confidence:
           → action = update (patch summary/tags/confidence on existing)
         no conflict AND candidate.confidence < existing.confidence:
           → action = merge (append candidate summary to existing; keep existing conf)

  5. Update candidate record with outcome.
  6. Return PromoteResult.

Stale-marking: if the action is supersede, the old node's status is set to
'superseded' and a MemoryRelation(new, supersedes, old) is created.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    ConflictKind,
    DuplicateMatch,
    Evidence,
    MemoryNode,
    MemoryRelation,
    MemoryStatus,
    PersistedCandidate,
    PlacementDecision,
    PromoteAction,
    PromoteResult,
    RelationType,
)
from memory_engine.repositories.candidate import CandidateRepository
from memory_engine.repositories.evidence import EvidenceRepository
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.repositories.project import ProjectRepository
from memory_engine.repositories.relation import RelationRepository
from memory_engine.services.conflict import ConflictService
from memory_engine.services.consolidation import ConsolidationService
from memory_engine.services.deduplication import DeduplicationService
from memory_engine.services.placement import PlacementService


class CandidateNotFoundError(KeyError):
    pass


class ProjectNotFoundError(KeyError):
    pass


class PromotionService:
    def __init__(self, session: Session) -> None:
        self._candidates = CandidateRepository(session)
        self._nodes = MemoryNodeRepository(session)
        self._evidence = EvidenceRepository(session)
        self._relations = RelationRepository(session)
        self._projects = ProjectRepository(session)
        self._placement = PlacementService(session)
        self._dedup = DeduplicationService()
        self._conflict = ConflictService()
        self._consolidation = ConsolidationService(session)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def promote(self, candidate_id: str) -> PromoteResult:
        """Run the full promotion pipeline for a pending candidate."""

        candidate_orm = self._candidates.get(candidate_id)
        if candidate_orm is None:
            raise CandidateNotFoundError(candidate_id)

        candidate = PersistedCandidate.model_validate(candidate_orm)

        # Verify project exists
        project_orm = self._projects.get(str(candidate.project_id))
        if project_orm is None:
            raise ProjectNotFoundError(str(candidate.project_id))

        # 1. Placement
        placement = self._placement.decide(
            candidate, project_id=str(candidate.project_id)
        )

        # 2. Deduplication
        all_nodes = [
            MemoryNode.model_validate(o)
            for o in self._nodes.list_by_project(str(candidate.project_id))
        ]
        duplicates = self._dedup.find_duplicates(candidate, all_nodes)

        # 3. Branch on similarity
        result = self._branch(candidate, placement, duplicates)

        # 4. Persist candidate outcome
        self._candidates.update_outcome(
            candidate_id,
            status=(
                "needs_review"
                if result.needs_human_review
                else ("promoted" if result.action != PromoteAction.discard else "discarded")
            ),
            promote_action=result.action.value,
            target_node_id=str(result.target_node.id) if result.target_node else None,
        )

        # 5. Invalidate unified-context cache (promotion changes what agents see)
        self._invalidate_cache(str(candidate.project_id))

        return result

    def mark_stale(self, node_id: str, reason: str) -> MemoryNode:
        """Directly mark a node as stale (callable from CLI and API)."""
        updated = self._nodes.update_status(node_id, "stale")
        if updated is None:
            from memory_engine.services.memory_service import MemoryNodeNotFoundError
            raise MemoryNodeNotFoundError(node_id)
        node = MemoryNode.model_validate(updated)
        # Stale mark changes what recall returns — invalidate cache
        self._invalidate_cache(str(node.project_id))
        return node

    def _invalidate_cache(self, project_id: str) -> None:
        """Invalidate the unified-context cache for the given project.

        Imported lazily to avoid a hard circular dependency between
        services and knowledge.cache.
        """
        try:
            from memory_engine.knowledge.cache import get_global_cache
            get_global_cache().invalidate_project(project_id)
        except Exception:
            pass  # cache invalidation is best-effort; never crash promotion

    # ------------------------------------------------------------------
    # Pipeline branches
    # ------------------------------------------------------------------

    def _branch(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicates: list[DuplicateMatch],
    ) -> PromoteResult:

        # ── No duplicate ────────────────────────────────────────────────
        if not duplicates:
            return self._action_create(candidate, placement, None)

        best = duplicates[0]

        # ── Near-duplicate (≥ 0.8) ──────────────────────────────────────
        if self._dedup.is_near_duplicate(best):
            conflict = self._conflict.evaluate(candidate, best)
            if conflict:
                return self._action_needs_review(
                    candidate, placement, best, conflict.reason
                )
            # No conflict
            if candidate.confidence >= best.existing_node.confidence:
                return self._action_supersede(candidate, placement, best)
            else:
                return self._action_discard(candidate, placement, best)

        # ── Partial match (0.5 – 0.8) ───────────────────────────────────
        if self._dedup.is_partial_match(best):
            conflict = self._conflict.evaluate(candidate, best)
            if conflict:
                return self._action_needs_review(
                    candidate, placement, best, conflict.reason
                )
            if candidate.confidence >= best.existing_node.confidence:
                return self._action_update(candidate, placement, best)
            else:
                return self._action_merge(candidate, placement, best)

        # ── Below threshold — treat as no duplicate ──────────────────────
        return self._action_create(candidate, placement, None)

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    def _action_create(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicate: DuplicateMatch | None,
    ) -> PromoteResult:
        node = self._create_node(candidate, placement)
        evidence = self._attach_evidence(candidate, node)
        notes = self._consolidate(node)
        return PromoteResult(
            candidate_id=candidate.id,
            action=PromoteAction.create,
            target_node=node,
            placement=placement,
            duplicate_match=duplicate,
            conflict_report=None,
            relations_created=[],
            consolidation_notes=notes,
        )

    def _action_supersede(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicate: DuplicateMatch,
    ) -> PromoteResult:
        old_node = duplicate.existing_node

        # Create new node
        new_node = self._create_node(candidate, placement)
        self._attach_evidence(candidate, new_node)
        notes = self._consolidate(new_node)

        # Mark old node as superseded (traceable — not deleted)
        self._nodes.update_status(str(old_node.id), MemoryStatus.superseded.value)
        notes.append(
            f"Marked '{old_node.title}' as superseded "
            f"(confidence {old_node.confidence:.2f} → retired)."
        )

        # Create supersedes relation
        rels = self._safe_add_relation(
            str(new_node.id), str(old_node.id), RelationType.supersedes
        )

        return PromoteResult(
            candidate_id=candidate.id,
            action=PromoteAction.supersede,
            target_node=new_node,
            placement=placement,
            duplicate_match=duplicate,
            conflict_report=None,
            relations_created=rels,
            consolidation_notes=notes,
        )

    def _action_update(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicate: DuplicateMatch,
    ) -> PromoteResult:
        existing = duplicate.existing_node

        # Merge tags
        merged_tags = list(
            dict.fromkeys(list(existing.tags) + candidate.proposed_tags)
        )

        updated_orm = self._nodes.update_fields(
            str(existing.id),
            summary=candidate.summary,
            confidence=max(candidate.confidence, existing.confidence),
            importance=max(candidate.importance, existing.importance),
            tags=merged_tags,
        )
        updated_node = MemoryNode.model_validate(updated_orm) if updated_orm else existing

        self._attach_evidence_to_id(candidate, str(existing.id))
        notes = [f"Updated existing node '{existing.title}' with candidate content."]
        if placement.parent_id:
            notes += self._consolidation.update_parent(str(placement.parent_id))

        rels = self._safe_add_relation(
            str(updated_node.id), str(existing.id), RelationType.derived_from
        ) if str(updated_node.id) != str(existing.id) else []

        return PromoteResult(
            candidate_id=candidate.id,
            action=PromoteAction.update,
            target_node=updated_node,
            placement=placement,
            duplicate_match=duplicate,
            relations_created=rels,
            consolidation_notes=notes,
        )

    def _action_merge(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicate: DuplicateMatch,
    ) -> PromoteResult:
        existing = duplicate.existing_node

        # Append candidate summary (if meaningfully different) without losing
        # the original — never insert raw evidence
        new_summary = existing.summary
        cand_snippet = candidate.summary[:200]
        if cand_snippet.lower() not in existing.summary.lower():
            new_summary = f"{existing.summary} Additionally: {cand_snippet}"

        merged_tags = list(
            dict.fromkeys(list(existing.tags) + candidate.proposed_tags)
        )
        updated_orm = self._nodes.update_fields(
            str(existing.id),
            summary=new_summary,
            tags=merged_tags,
        )
        updated_node = MemoryNode.model_validate(updated_orm) if updated_orm else existing

        # Add a supports relation from this candidate perspective
        rels = self._safe_add_relation(
            str(existing.id), str(existing.id), RelationType.supports
        )  # self-loop intentionally avoided below
        rels = []  # candidate has no permanent node, so no relation needed

        notes = [
            f"Merged candidate summary into '{existing.title}' "
            f"(existing confidence {existing.confidence:.2f} preserved)."
        ]
        return PromoteResult(
            candidate_id=candidate.id,
            action=PromoteAction.merge,
            target_node=updated_node,
            placement=placement,
            duplicate_match=duplicate,
            relations_created=rels,
            consolidation_notes=notes,
        )

    def _action_discard(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicate: DuplicateMatch,
    ) -> PromoteResult:
        return PromoteResult(
            candidate_id=candidate.id,
            action=PromoteAction.discard,
            target_node=duplicate.existing_node,
            placement=placement,
            duplicate_match=duplicate,
            consolidation_notes=[
                f"Candidate discarded: existing node '{duplicate.existing_node.title}' "
                f"has equal or higher confidence ({duplicate.existing_node.confidence:.2f}) "
                f"and covers the same content."
            ],
        )

    def _action_needs_review(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
        duplicate: DuplicateMatch,
        reason: str,
    ) -> PromoteResult:
        # Mark existing node for human review so it surfaces in queries
        self._nodes.update_status(
            str(duplicate.existing_node.id), MemoryStatus.needs_review.value
        )
        return PromoteResult(
            candidate_id=candidate.id,
            action=PromoteAction.needs_review,
            target_node=duplicate.existing_node,
            placement=placement,
            duplicate_match=duplicate,
            needs_human_review=True,
            review_reason=reason,
            consolidation_notes=[
                f"Existing node '{duplicate.existing_node.title}' flagged for human review."
            ],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_node(
        self,
        candidate: PersistedCandidate,
        placement: PlacementDecision,
    ):  # type: ignore[return]
        from memory_engine.config import settings

        depth = placement.intended_depth
        if depth > settings.max_tree_depth:
            depth = settings.max_tree_depth

        orm = self._nodes.create(
            project_id=str(candidate.project_id),
            parent_id=str(placement.parent_id) if placement.parent_id else None,
            title=candidate.title,
            summary=candidate.summary,
            kind=candidate.proposed_kind,
            depth=depth,
            tags=candidate.proposed_tags,
            status="active",
            confidence=candidate.confidence,
            importance=candidate.importance,
            module_path=candidate.proposed_module_path,
        )
        return MemoryNode.model_validate(orm)

    def _attach_evidence(
        self, candidate: PersistedCandidate, node: MemoryNode
    ) -> Evidence | None:
        if not candidate.evidence_content:
            return None
        orm = self._evidence.create(
            memory_node_id=str(node.id),
            content=candidate.evidence_content,
            source=candidate.evidence_source,
        )
        return Evidence.model_validate(orm)

    def _attach_evidence_to_id(
        self, candidate: PersistedCandidate, node_id: str
    ) -> None:
        if candidate.evidence_content:
            self._evidence.create(
                memory_node_id=node_id,
                content=candidate.evidence_content,
                source=candidate.evidence_source,
            )

    def _consolidate(self, node: MemoryNode) -> list[str]:
        if node.parent_id is not None:
            return self._consolidation.update_parent(str(node.parent_id))
        return []

    def _safe_add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: RelationType,
    ) -> list[MemoryRelation]:
        """Add a relation, silently ignoring duplicates."""
        if source_id == target_id:
            return []
        try:
            orm = self._relations.create(
                source_id=source_id,
                target_id=target_id,
                relation_type=relation_type.value,
            )
            return [MemoryRelation.model_validate(orm)]
        except Exception:
            # Unique constraint violation — relation already exists
            return []
