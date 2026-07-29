"""SourceValidityService — automatic stale-memory detection (Issue 1, Phase 15).

Problem: a source-backed memory (e.g. an architecture note linked to
``src/auth/service.py``) can remain ``active`` with high confidence long
after its supporting file is deleted or materially rewritten. Retrieval then
keeps presenting stale information as authoritative.

Design constraints (see phase spec):
  - Conservative: only nodes that carry explicit source evidence
    (``source_path`` set) are ever touched. Legacy nodes without this
    metadata are left completely alone — no behavior change for existing
    databases.
  - Cheap: this only does a file-existence check plus (optionally) a
    sha256 hash comparison for a *bounded* set of already-shortlisted
    candidates. No repository-wide scans, no git history walks.
  - Auditable: every automatic transition is recorded via
    MemoryNodeRepository.set_validity(), which preserves the previous
    status, a human-readable reason, and a timestamp. Nothing is deleted.
  - Reversible: revalidate() can bring a node back to ``active`` when its
    evidence is re-established (e.g. the file is restored).

Lifecycle produced by this service (subset of MemoryStatus):
  active              -> unchanged when source still resolves and hash matches
  needs_revalidation  -> source file exists but content hash changed
  invalidated         -> source file no longer exists at the recorded path
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from memory_engine.models.domain import MemoryNode, MemoryStatus

# Statuses a validity check must never override — the node has already left
# the "authoritative or needs-checking" lifecycle for an unrelated reason.
_TERMINAL_STATUSES = frozenset({
    MemoryStatus.superseded,
    MemoryStatus.archived,
})


@dataclass(frozen=True)
class ValidityCheck:
    """Result of checking one node's source evidence."""

    changed: bool
    new_status: MemoryStatus | None = None
    reason: str | None = None
    new_source_hash: str | None = None


def hash_file(path: Path) -> str | None:
    """sha256 hex digest of a file's bytes, or None if it cannot be read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


class SourceValidityService:
    """Conservative, lazy source-validity checker.

    Stateless — safe to construct per request. All filesystem access is
    bounded to the single ``source_path`` of the node under check.
    """

    def _resolve(self, project_root: Path, source_path: str) -> Path:
        p = Path(source_path)
        if p.is_absolute():
            return p
        return project_root / p

    def check(self, node: MemoryNode, project_root: Path) -> ValidityCheck:
        """Decide whether ``node`` should transition to a different status.

        Returns a no-op ValidityCheck (changed=False) when:
          - the node has no source_path (nothing to validate — legacy memory);
          - the node is already in a terminal status (superseded/archived);
          - the source file still exists and its hash is unchanged or unknown.
        """
        if not node.source_path:
            return ValidityCheck(changed=False)

        if node.status in _TERMINAL_STATUSES:
            return ValidityCheck(changed=False)

        resolved = self._resolve(project_root, node.source_path)

        if not resolved.exists():
            if node.status == MemoryStatus.invalidated:
                return ValidityCheck(changed=False)
            return ValidityCheck(
                changed=True,
                new_status=MemoryStatus.invalidated,
                reason=f"source file no longer exists: {node.source_path}",
            )

        # File exists. If we have no recorded hash, we cannot detect content
        # drift — conservatively leave the node alone rather than guess.
        if not node.source_hash:
            return ValidityCheck(changed=False)

        current_hash = hash_file(resolved)
        if current_hash is None or current_hash == node.source_hash:
            return ValidityCheck(changed=False)

        if node.status == MemoryStatus.needs_revalidation:
            return ValidityCheck(changed=False)

        return ValidityCheck(
            changed=True,
            new_status=MemoryStatus.needs_revalidation,
            reason=f"source content changed since memory was written: {node.source_path}",
        )

    def revalidate(self, node: MemoryNode, project_root: Path) -> ValidityCheck:
        """Explicitly re-check a non-active node's evidence and, if the
        source file now exists and is readable, restore it to ``active``
        with an updated hash. This is the only path back to ``active`` for
        an automatically-invalidated node — a human or explicit process
        must trigger it (never happens implicitly during recall).
        """
        if not node.source_path:
            return ValidityCheck(changed=False)

        resolved = self._resolve(project_root, node.source_path)
        if not resolved.exists():
            return ValidityCheck(changed=False)

        current_hash = hash_file(resolved)
        if current_hash is None:
            return ValidityCheck(changed=False)

        if node.status == MemoryStatus.active and current_hash == node.source_hash:
            return ValidityCheck(changed=False)

        return ValidityCheck(
            changed=True,
            new_status=MemoryStatus.active,
            reason=f"source evidence re-established: {node.source_path}",
            new_source_hash=current_hash,
        )
