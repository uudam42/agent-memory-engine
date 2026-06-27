"""Memory Retention & Compaction Service (Phase 11).

Lifecycle states (MemoryNodeORM.status):
  active       — current, eligible for normal retrieval
  stale        — potentially outdated; penalized in retrieval
  superseded   — replaced by newer verified memory; excluded by default
  needs_review — conflict or uncertain validity; excluded by default
  archived     — historical/audit-only; excluded from default retrieval
  compacted    — synthesis of source memories; eligible with lineage
  candidate    — staging (MemoryCandidateORM.status = 'pending')
  expired      — candidate never promoted; archived (MemoryCandidateORM.status)

Safety invariants:
  - Protected types (constraint, security_rule, architecture, decision)
    are never auto-archived or auto-compacted.
  - Compaction always preserves source-memory IDs and evidence IDs.
  - No physical DELETE; all transitions are lifecycle state changes.
  - All dry_run calls mutate nothing.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_
from sqlalchemy.orm import Session

from memory_engine.models.orm import (
    EvidenceORM,
    MemoryCandidateORM,
    MemoryNodeORM,
    MemoryRelationORM,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Protected types — never auto-archived or auto-compacted
# ---------------------------------------------------------------------------

_DEFAULT_PROTECTED_TYPES: frozenset[str] = frozenset(
    {"constraint", "security_rule", "architecture", "decision"}
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RetentionAction:
    memory_id: str
    title: str
    action: str          # "archive" | "expire_candidate" | "compact_source"
    reason: str
    dry_run: bool = True


@dataclass
class CompactionResult:
    compacted_memory_id: str
    title: str
    source_memory_ids: list[str]
    source_evidence_ids: list[str]
    dry_run: bool = True


@dataclass
class RetentionReport:
    ran_at: str
    dry_run: bool
    active_count: int = 0
    archived_count: int = 0
    stale_count: int = 0
    superseded_count: int = 0
    needs_review_count: int = 0
    compacted_count: int = 0
    candidate_pending_count: int = 0
    candidate_expired_count: int = 0
    compaction_groups_found: int = 0
    archive_actions: list[RetentionAction] = field(default_factory=list)
    expiry_actions: list[RetentionAction] = field(default_factory=list)
    compaction_results: list[CompactionResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "dry_run": self.dry_run,
            "counts": {
                "active": self.active_count,
                "archived": self.archived_count,
                "stale": self.stale_count,
                "superseded": self.superseded_count,
                "needs_review": self.needs_review_count,
                "compacted": self.compacted_count,
                "candidate_pending": self.candidate_pending_count,
                "candidate_expired": self.candidate_expired_count,
                "compaction_groups_found": self.compaction_groups_found,
            },
            "archive_actions": [
                {"id": a.memory_id, "title": a.title, "reason": a.reason}
                for a in self.archive_actions
            ],
            "expiry_actions": [
                {"id": a.memory_id, "title": a.title, "reason": a.reason}
                for a in self.expiry_actions
            ],
            "compaction_results": [
                {
                    "compacted_id": r.compacted_memory_id,
                    "title": r.title,
                    "source_count": len(r.source_memory_ids),
                    "source_ids": r.source_memory_ids,
                }
                for r in self.compaction_results
            ],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# MemoryRetentionService
# ---------------------------------------------------------------------------


class MemoryRetentionService:
    """Manages memory lifecycle: candidate expiry, archival, and compaction.

    All destructive-looking operations are lifecycle transitions only —
    no records are physically deleted.  Protected types are always excluded
    from auto-archive and auto-compaction.
    """

    def __init__(
        self,
        session: Session,
        project_id: str,
        *,
        candidate_expiry_days: int = 30,
        inactive_archive_days: int = 120,
        stale_archive_days: int = 180,
        compaction_min_group: int = 3,
        compaction_min_age_days: int = 14,
        auto_archive_compacted_sources: bool = True,
        protected_types: frozenset[str] | None = None,
    ) -> None:
        self._session = session
        self._project_id = project_id
        self._candidate_expiry_days = candidate_expiry_days
        self._inactive_archive_days = inactive_archive_days
        self._stale_archive_days = stale_archive_days
        self._compaction_min_group = compaction_min_group
        self._compaction_min_age_days = compaction_min_age_days
        self._auto_archive_compacted_sources = auto_archive_compacted_sources
        self._protected = protected_types if protected_types is not None else _DEFAULT_PROTECTED_TYPES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_report(self) -> RetentionReport:
        """Return a read-only diagnostic report — no mutations."""
        report = RetentionReport(ran_at=_now().isoformat(), dry_run=True)
        self._fill_counts(report)
        report.expiry_actions = self.identify_expired_candidates(dry_run=True)
        report.archive_actions = self.identify_archive_candidates(dry_run=True)
        groups = self.identify_compaction_groups()
        report.compaction_groups_found = len(groups)
        return report

    def run(self, *, dry_run: bool = True) -> RetentionReport:
        """Execute a full retention cycle.

        When dry_run=True (default) no state is mutated.
        """
        report = RetentionReport(ran_at=_now().isoformat(), dry_run=dry_run)
        self._fill_counts(report)

        report.expiry_actions = self.identify_expired_candidates(dry_run=dry_run)
        report.archive_actions = self.identify_archive_candidates(dry_run=dry_run)

        groups = self.identify_compaction_groups()
        report.compaction_groups_found = len(groups)
        if not dry_run:
            for group in groups:
                result = self.compact_memory_group(group, dry_run=False)
                if result:
                    report.compaction_results.append(result)

        if not dry_run:
            self._session.flush()
        self._fill_counts(report)
        return report

    def identify_expired_candidates(self, *, dry_run: bool = True) -> list[RetentionAction]:
        """Find and optionally expire candidates unpromoted beyond expiry window."""
        cutoff = _now() - timedelta(days=self._candidate_expiry_days)
        candidates = (
            self._session.query(MemoryCandidateORM)
            .filter(
                and_(
                    MemoryCandidateORM.project_id == self._project_id,
                    MemoryCandidateORM.status == "pending",
                    MemoryCandidateORM.created_at < cutoff,
                )
            )
            .all()
        )
        actions: list[RetentionAction] = []
        for c in candidates:
            reason = (
                f"Candidate unpromoted for >{self._candidate_expiry_days} days "
                f"(created {c.created_at.date()})"
            )
            actions.append(RetentionAction(
                memory_id=c.id,
                title=c.title,
                action="expire_candidate",
                reason=reason,
                dry_run=dry_run,
            ))
            if not dry_run:
                c.status = "expired"
                c.expiry_reason = reason
                c.expires_at = _now()
        if not dry_run:
            self._session.flush()
        return actions

    def identify_archive_candidates(self, *, dry_run: bool = True) -> list[RetentionAction]:
        """Find and optionally archive active memories eligible for archival."""
        actions: list[RetentionAction] = []
        now = _now()

        # 1. Stale memories past stale_archive_days
        stale_cutoff = now - timedelta(days=self._stale_archive_days)
        stale_nodes = (
            self._session.query(MemoryNodeORM)
            .filter(
                and_(
                    MemoryNodeORM.project_id == self._project_id,
                    MemoryNodeORM.status == "stale",
                    MemoryNodeORM.updated_at < stale_cutoff,
                    MemoryNodeORM.kind.notin_(list(self._protected)),
                )
            )
            .all()
        )
        for node in stale_nodes:
            reason = (
                f"Stale memory unchanged for >{self._stale_archive_days} days"
            )
            actions.append(RetentionAction(
                memory_id=node.id,
                title=node.title,
                action="archive",
                reason=reason,
                dry_run=dry_run,
            ))
            if not dry_run:
                self._archive_node(node, reason)
                self._session.flush()

        # 2. Superseded memories past inactive_archive_days
        inactive_cutoff = now - timedelta(days=self._inactive_archive_days)
        superseded_nodes = (
            self._session.query(MemoryNodeORM)
            .filter(
                and_(
                    MemoryNodeORM.project_id == self._project_id,
                    MemoryNodeORM.status == "superseded",
                    MemoryNodeORM.updated_at < inactive_cutoff,
                    MemoryNodeORM.kind.notin_(list(self._protected)),
                )
            )
            .all()
        )
        for node in superseded_nodes:
            reason = (
                f"Superseded memory inactive for >{self._inactive_archive_days} days"
            )
            actions.append(RetentionAction(
                memory_id=node.id,
                title=node.title,
                action="archive",
                reason=reason,
                dry_run=dry_run,
            ))
            if not dry_run:
                self._archive_node(node, reason)
                self._session.flush()

        return actions

    def identify_compaction_groups(self) -> list[list[MemoryNodeORM]]:
        """Return lists of memory nodes that are candidates for compaction.

        Grouping criteria (all must be satisfied):
        - same project
        - same kind (memory type)
        - same parent_id (same parent scope)
        - status == 'active'
        - kind not in protected types
        - age >= compaction_min_age_days
        - group size >= compaction_min_group
        """
        min_age_cutoff = _now() - timedelta(days=self._compaction_min_age_days)

        nodes = (
            self._session.query(MemoryNodeORM)
            .filter(
                and_(
                    MemoryNodeORM.project_id == self._project_id,
                    MemoryNodeORM.status == "active",
                    MemoryNodeORM.kind.notin_(list(self._protected)),
                    MemoryNodeORM.created_at < min_age_cutoff,
                )
            )
            .all()
        )

        # Group by (parent_id, kind)
        groups_map: dict[tuple[str | None, str], list[MemoryNodeORM]] = {}
        for node in nodes:
            key = (node.parent_id, node.kind)
            groups_map.setdefault(key, []).append(node)

        return [
            group
            for group in groups_map.values()
            if len(group) >= self._compaction_min_group
        ]

    def compact_memory_group(
        self,
        nodes: list[MemoryNodeORM],
        *,
        dry_run: bool = True,
    ) -> CompactionResult | None:
        """Compact a group of related memories into a single summary node.

        The original nodes are linked to the compacted node via
        memory_relations with relation_type='compaction_source'.
        Source nodes are optionally archived; they are never deleted.
        """
        if len(nodes) < self._compaction_min_group:
            return None

        # Collect evidence IDs before any mutation
        source_ids = [n.id for n in nodes]
        evidence_ids: list[str] = []
        for node in nodes:
            evidence = (
                self._session.query(EvidenceORM)
                .filter_by(memory_node_id=node.id)
                .all()
            )
            evidence_ids.extend(e.id for e in evidence)

        # Build compacted summary
        combined_titles = "; ".join(n.title for n in nodes[:5])
        if len(nodes) > 5:
            combined_titles += f" (+{len(nodes) - 5} more)"
        title = f"[Compacted] {nodes[0].kind.title()} group: {combined_titles}"[:256]
        summary_parts = [f"Compacted from {len(nodes)} related memories.\n"]
        for n in nodes:
            summary_parts.append(f"- {n.title}: {n.summary[:200]}")
        summary = "\n".join(summary_parts)

        avg_confidence = sum(n.confidence for n in nodes) / len(nodes)
        avg_importance = sum(n.importance for n in nodes) / len(nodes)
        parent_id = nodes[0].parent_id
        kind = nodes[0].kind

        result = CompactionResult(
            compacted_memory_id="",
            title=title,
            source_memory_ids=source_ids,
            source_evidence_ids=evidence_ids,
            dry_run=dry_run,
        )

        if dry_run:
            result.compacted_memory_id = f"(dry-run:{hashlib.sha256(title.encode()).hexdigest()[:8]})"
            return result

        # Create the compacted node
        compacted_id = str(uuid.uuid4())
        compacted = MemoryNodeORM(
            id=compacted_id,
            project_id=self._project_id,
            parent_id=parent_id,
            title=title,
            summary=summary,
            kind=kind,
            depth=nodes[0].depth,
            tags=["compacted"],
            status="compacted",
            confidence=avg_confidence,
            importance=avg_importance,
            branch_scope="global",
        )
        self._session.add(compacted)
        self._session.flush()

        # Link source memories to compacted node
        for node in nodes:
            node.compacted_into_id = compacted_id
            rel = MemoryRelationORM(
                source_id=compacted_id,
                target_id=node.id,
                relation_type="compaction_source",
            )
            self._session.add(rel)

            if self._auto_archive_compacted_sources:
                self._archive_node(
                    node,
                    f"Archived after compaction into {compacted_id}",
                )

        self._session.flush()
        result.compacted_memory_id = compacted_id
        return result

    def archive_memory(self, memory_id: str, reason: str) -> MemoryNodeORM | None:
        """Manually archive a single memory node."""
        node = self._session.get(MemoryNodeORM, memory_id)
        if node is None or node.project_id != self._project_id:
            return None
        self._archive_node(node, reason)
        self._session.flush()
        return node

    def restore_memory(self, memory_id: str) -> MemoryNodeORM | None:
        """Restore an archived memory to active status."""
        node = self._session.get(MemoryNodeORM, memory_id)
        if node is None or node.project_id != self._project_id:
            return None
        node.status = "active"
        node.archived_at = None
        node.archived_reason = None
        self._session.flush()
        return node

    def evaluate_branch_lifecycle(
        self,
        *,
        current_branches: set[str],
        dry_run: bool = True,
        archive_after_days: int = 30,
    ) -> list[RetentionAction]:
        """Archive branch-local memories for branches no longer active.

        Uses read-only branch set (caller resolves from Git).
        Mainline/global scoped memory is never touched.
        Promoted memories (branch_promotion_eligible=True) are preserved.
        """
        actions: list[RetentionAction] = []
        cutoff = _now() - timedelta(days=archive_after_days)

        branch_nodes = (
            self._session.query(MemoryNodeORM)
            .filter(
                and_(
                    MemoryNodeORM.project_id == self._project_id,
                    MemoryNodeORM.status == "active",
                    MemoryNodeORM.branch_scope == "current_branch",
                    MemoryNodeORM.branch_promotion_eligible == 0,
                    MemoryNodeORM.updated_at < cutoff,
                )
            )
            .all()
        )

        for node in branch_nodes:
            if node.branch_name and node.branch_name in current_branches:
                continue  # branch still alive
            reason = (
                f"Branch '{node.branch_name}' no longer active; "
                f"memory inactive for >{archive_after_days} days"
            )
            actions.append(RetentionAction(
                memory_id=node.id,
                title=node.title,
                action="archive",
                reason=reason,
                dry_run=dry_run,
            ))
            if not dry_run:
                self._archive_node(node, reason)
                self._session.flush()

        return actions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _archive_node(self, node: MemoryNodeORM, reason: str) -> None:
        node.status = "archived"
        node.archived_at = _now()
        node.archived_reason = reason

    def _fill_counts(self, report: RetentionReport) -> None:
        for status, attr in [
            ("active", "active_count"),
            ("archived", "archived_count"),
            ("stale", "stale_count"),
            ("superseded", "superseded_count"),
            ("needs_review", "needs_review_count"),
            ("compacted", "compacted_count"),
        ]:
            count = (
                self._session.query(MemoryNodeORM)
                .filter_by(project_id=self._project_id, status=status)
                .count()
            )
            setattr(report, attr, count)

        report.candidate_pending_count = (
            self._session.query(MemoryCandidateORM)
            .filter_by(project_id=self._project_id, status="pending")
            .count()
        )
        report.candidate_expired_count = (
            self._session.query(MemoryCandidateORM)
            .filter_by(project_id=self._project_id, status="expired")
            .count()
        )
