"""MemoryNode repository — raw DB access, no business logic."""

from __future__ import annotations

from sqlalchemy.orm import Session, selectinload

from memory_engine.models.orm import MemoryNodeORM, _now


class MemoryNodeRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        project_id: str,
        parent_id: str | None,
        title: str,
        summary: str,
        kind: str,
        depth: int,
        tags: list[str],
        status: str = "active",
        confidence: float = 1.0,
        importance: float = 0.5,
        module_path: str | None = None,
        source_path: str | None = None,
        source_hash: str | None = None,
        source_symbol: str | None = None,
        constraint_scope: str | None = None,
        constraint_scope_ref: str | None = None,
        trust_level: str | None = None,
        evidence_level: str | None = None,
        verification_evidence: dict | None = None,
    ) -> MemoryNodeORM:
        obj = MemoryNodeORM(
            project_id=project_id,
            parent_id=parent_id,
            title=title,
            summary=summary,
            kind=kind,
            depth=depth,
            tags=tags,
            status=status,
            confidence=confidence,
            importance=importance,
            module_path=module_path,
            source_path=source_path,
            source_hash=source_hash,
            source_symbol=source_symbol,
            constraint_scope=constraint_scope,
            constraint_scope_ref=constraint_scope_ref,
            trust_level=trust_level,
            evidence_level=evidence_level,
            verification_evidence=verification_evidence,
        )
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def get(self, node_id: str) -> MemoryNodeORM | None:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(MemoryNodeORM.id == node_id)
            .first()
        )

    def get_bare(self, node_id: str) -> MemoryNodeORM | None:
        """Get node without eagerly loading evidence (for summary updates)."""
        return self._s.get(MemoryNodeORM, node_id)

    def list_by_project(self, project_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(MemoryNodeORM.project_id == project_id)
            .order_by(MemoryNodeORM.depth, MemoryNodeORM.created_at)
            .all()
        )

    def set_validity(
        self,
        node_id: str,
        *,
        new_status: str,
        reason: str,
        new_source_hash: str | None = None,
        actor: str = "source_validity_service",
    ) -> MemoryNodeORM | None:
        """Auditable lifecycle transition applied by SourceValidityService.

        Records previous_status before overwriting status, so the transition
        is reconstructable later (Issue 1 revalidation-audit requirement).
        Does not delete or otherwise touch the node's content.
        """
        obj = self._s.get(MemoryNodeORM, node_id)
        if obj is None:
            return None
        obj.previous_status = obj.status
        obj.status = new_status
        obj.validity_reason = f"[{actor}] {reason}"
        obj.validity_checked_at = _now()
        if new_source_hash is not None:
            obj.source_hash = new_source_hash
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def set_trust(
        self,
        node_id: str,
        *,
        new_trust: str,
        reason: str,
        actor: str = "source_trust_service",
        elevated: bool = False,
    ) -> MemoryNodeORM | None:
        """Auditable trust-level transition (Issue 3).

        Follows the exact same convention as ``set_validity``: records the
        previous trust level before overwriting, so the transition is
        reconstructable later. ``elevated`` additionally stamps the
        human-elevation audit fields — callers set it only when the
        transition raises trust (see
        ``memory_engine.services.source_trust.apply_trust_transition``).
        Never deletes or otherwise touches the node's content.
        """
        obj = self._s.get(MemoryNodeORM, node_id)
        if obj is None:
            return None
        obj.previous_trust = obj.trust_level
        obj.trust_level = new_trust
        obj.trust_reason = f"[{actor}] {reason}"
        obj.trust_set_at = _now()
        if elevated:
            obj.trust_elevated_by = actor
            obj.trust_elevated_reason = reason
            obj.trust_elevated_at = _now()
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def set_evidence_level(
        self,
        node_id: str,
        *,
        new_level: str,
        reason: str,
        actor: str = "verification_evidence_service",
        elevated: bool = False,
    ) -> MemoryNodeORM | None:
        """Auditable verification-evidence-level transition (Issue 4).

        Follows the exact same convention as ``set_trust``/``set_validity``:
        records the previous level before overwriting, so the transition is
        reconstructable later. ``elevated`` additionally stamps the
        human-elevation audit fields — callers set it only when the
        transition raises the level (see
        ``memory_engine.services.verification_evidence.apply_verification_transition``).
        Never deletes or otherwise touches the node's content.
        """
        obj = self._s.get(MemoryNodeORM, node_id)
        if obj is None:
            return None
        obj.previous_evidence_level = obj.evidence_level
        obj.evidence_level = new_level
        obj.evidence_reason = f"[{actor}] {reason}"
        obj.evidence_set_at = _now()
        if elevated:
            obj.evidence_elevated_by = actor
            obj.evidence_elevated_reason = reason
            obj.evidence_elevated_at = _now()
        self._s.add(obj)
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def list_active_by_project(self, project_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(
                MemoryNodeORM.project_id == project_id,
                MemoryNodeORM.status == "active",
            )
            .order_by(MemoryNodeORM.depth, MemoryNodeORM.created_at)
            .all()
        )

    def list_by_project_and_statuses(
        self, project_id: str, statuses: list[str]
    ) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(
                MemoryNodeORM.project_id == project_id,
                MemoryNodeORM.status.in_(statuses),
            )
            .order_by(MemoryNodeORM.depth, MemoryNodeORM.created_at)
            .all()
        )

    def list_children(self, parent_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .options(selectinload(MemoryNodeORM.evidence))
            .filter(MemoryNodeORM.parent_id == parent_id)
            .order_by(MemoryNodeORM.created_at)
            .all()
        )

    def list_roots(self, project_id: str) -> list[MemoryNodeORM]:
        return (
            self._s.query(MemoryNodeORM)
            .filter(
                MemoryNodeORM.project_id == project_id,
                MemoryNodeORM.parent_id.is_(None),
            )
            .order_by(MemoryNodeORM.created_at)
            .all()
        )

    # ------------------------------------------------------------------
    # Phase 3 — mutation helpers
    # ------------------------------------------------------------------

    def update_status(self, node_id: str, status: str) -> MemoryNodeORM | None:
        obj = self.get_bare(node_id)
        if obj is None:
            return None
        obj.status = status
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def update_summary(self, node_id: str, summary: str) -> MemoryNodeORM | None:
        """Replace the summary of a node (used by ConsolidationService)."""
        obj = self.get_bare(node_id)
        if obj is None:
            return None
        obj.summary = summary
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def update_fields(
        self,
        node_id: str,
        *,
        summary: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        branch_name: str | None = None,
        constraint_scope: str | None = None,
        constraint_scope_ref: str | None = None,
    ) -> MemoryNodeORM | None:
        """Patch a subset of mutable fields."""
        obj = self.get_bare(node_id)
        if obj is None:
            return None
        if summary is not None:
            obj.summary = summary
        if confidence is not None:
            obj.confidence = confidence
        if importance is not None:
            obj.importance = importance
        if tags is not None:
            obj.tags = tags
        if status is not None:
            obj.status = status
        if branch_name is not None:
            obj.branch_name = branch_name
        if constraint_scope is not None:
            obj.constraint_scope = constraint_scope
        if constraint_scope_ref is not None:
            obj.constraint_scope_ref = constraint_scope_ref
        self._s.commit()
        self._s.refresh(obj)
        return obj

    def delete(self, node_id: str) -> bool:
        obj = self._s.get(MemoryNodeORM, node_id)
        if obj is None:
            return False
        self._s.delete(obj)
        self._s.commit()
        return True
