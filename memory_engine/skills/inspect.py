"""InspectService — progressive drill-down for a specific memory node.

Use this when high-level recall summaries are insufficient.
The agent selects a node to inspect after initial recall.

Inspection:
  1. Return the selected node.
  2. Return direct children (up to inspection_depth levels).
  3. Return related memories via MemoryRelation.
  4. Return linked evidence (if include_evidence=True).
  5. Flag conflicts: sibling/related nodes with stale or superseded status.
  6. Compute confidence assessment with freshness decay.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    ConfidenceAssessment,
    Evidence,
    InspectRequest,
    InspectResult,
    MemoryNode,
    MemoryStatus,
)
from memory_engine.repositories.evidence import EvidenceRepository
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.repositories.relation import RelationRepository
from memory_engine.services.memory_service import MemoryNodeNotFoundError
from memory_engine.skills.ranker import _freshness  # reuse freshness formula

_FRESHNESS_HALF_LIFE = 180.0


class InspectService:
    def __init__(self, session: Session) -> None:
        self._nodes = MemoryNodeRepository(session)
        self._relations = RelationRepository(session)
        self._evidence = EvidenceRepository(session)

    def inspect(self, request: InspectRequest) -> InspectResult:
        """Drill down into a memory node and its neighborhood."""

        # Load the target node
        target_orm = self._nodes.get(request.memory_id)
        if target_orm is None:
            raise MemoryNodeNotFoundError(request.memory_id)
        target = MemoryNode.model_validate(target_orm)

        trace: list[str] = [f"Inspecting node: {target.title} [{target.kind}]"]

        # Children up to inspection_depth
        children: list[MemoryNode] = []
        self._collect_children(
            request.memory_id,
            depth_remaining=request.inspection_depth,
            result=children,
            trace=trace,
        )

        # Related memories via MemoryRelation
        related_memories: list[MemoryNode] = []
        conflicts: list[MemoryNode] = []
        rel_orms = self._relations.list_by_node(request.memory_id)
        trace.append(f"Found {len(rel_orms)} relation(s) for this node.")

        for rel in rel_orms:
            peer_id = (
                rel.target_id
                if rel.source_id == request.memory_id
                else rel.source_id
            )
            peer_orm = self._nodes.get(peer_id)
            if peer_orm is None:
                continue
            peer = MemoryNode.model_validate(peer_orm)
            if peer.status in (MemoryStatus.stale, MemoryStatus.superseded):
                conflicts.append(peer)
                trace.append(
                    f"Conflict detected: '{peer.title}' has status={peer.status.value} "
                    f"(relation: {rel.relation_type})"
                )
            else:
                related_memories.append(peer)
                trace.append(
                    f"Related: '{peer.title}' [{peer.kind}] via {rel.relation_type}"
                )

        # Evidence (only if requested, and only minimum needed)
        evidence_refs: list[Evidence] = []
        if request.include_evidence:
            ev_orms = self._evidence.list_by_node(request.memory_id)
            evidence_refs = [Evidence.model_validate(e) for e in ev_orms]
            trace.append(f"Loaded {len(evidence_refs)} evidence item(s).")

        # Confidence assessment
        freshness_score = _freshness(target.updated_at)
        assessment = ConfidenceAssessment(
            confidence=target.confidence,
            status=target.status,
            freshness=freshness_score,
        )
        trace.append(
            f"Confidence assessment: confidence={target.confidence:.2f}, "
            f"freshness={freshness_score:.2f}, status={target.status.value}"
        )

        if target.status == MemoryStatus.stale:
            trace.append("WARNING: This memory node is stale — verify before acting on it.")
        if target.status == MemoryStatus.superseded:
            trace.append(
                "WARNING: This memory node has been superseded — it may describe outdated behavior."
            )

        return InspectResult(
            memory=target,
            children=children,
            related_memories=related_memories,
            evidence_refs=evidence_refs,
            conflicts=conflicts,
            inspection_trace=trace,
            confidence_assessment=assessment,
        )

    def _collect_children(
        self,
        node_id: str,
        depth_remaining: int,
        result: list[MemoryNode],
        trace: list[str],
    ) -> None:
        if depth_remaining <= 0:
            return
        child_orms = self._nodes.list_children(node_id)
        trace.append(f"  {len(child_orms)} child node(s) at next level.")
        for child_orm in child_orms:
            child = MemoryNode.model_validate(child_orm)
            result.append(child)
            self._collect_children(
                str(child.id), depth_remaining - 1, result, trace
            )
