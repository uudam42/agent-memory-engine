"""GitContext and GitRename domain models.

Immutable snapshot of the Git working tree state at a point in time.
Never contains credentials, remote URLs, Git user identity, or email addresses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class GitRename:
    """A file that was renamed in the working tree or staged area."""

    old_path: str
    new_path: str
    similarity_score: float | None = None


@dataclass(frozen=True)
class GitContext:
    """Immutable snapshot of Git repository state for a single project root.

    All paths are relative to repository_root.
    No credentials, remote URLs, user identities, or email addresses are stored.
    """

    repository_root: str
    git_available: bool
    is_repository: bool

    # Branch and commit information
    current_branch: str | None = None
    head_commit: str | None = None          # short SHA (8 chars) — not full SHA

    # Base branch determination
    base_branch: str | None = None
    base_commit: str | None = None
    upstream_branch: str | None = None

    # Repository state flags
    is_detached_head: bool = False
    working_tree_dirty: bool = False

    # Changed file lists (relative paths)
    staged_files: tuple[str, ...] = field(default_factory=tuple)
    modified_files: tuple[str, ...] = field(default_factory=tuple)
    untracked_files: tuple[str, ...] = field(default_factory=tuple)
    deleted_files: tuple[str, ...] = field(default_factory=tuple)
    renamed_files: tuple[GitRename, ...] = field(default_factory=tuple)

    # Repository revision identifier (short HEAD SHA when available)
    repository_revision: str | None = None

    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # ----------------------------------------------------------------
    # Derived helpers
    # ----------------------------------------------------------------

    def is_feature_branch(self) -> bool:
        """Return True if current branch looks like a feature/topic branch."""
        if not self.current_branch:
            return False
        mainline = {"main", "master", "develop", "development", "trunk"}
        if self.current_branch in mainline:
            return False
        return True

    def is_mainline(self) -> bool:
        """Return True if current branch is a known mainline branch."""
        if not self.current_branch:
            return False
        mainline = {"main", "master", "develop", "development", "trunk"}
        return self.current_branch in mainline

    def all_changed_paths(self) -> list[str]:
        """Return all paths that changed in any way (staged + modified + deleted + renamed new)."""
        paths: list[str] = []
        paths.extend(self.staged_files)
        paths.extend(self.modified_files)
        paths.extend(self.deleted_files)
        paths.extend(r.new_path for r in self.renamed_files)
        return list(dict.fromkeys(paths))  # deduplicate, preserve order

    def to_dict(self) -> dict:
        """Return a safe, compact dict for MCP resources and status outputs."""
        return {
            "git_available": self.git_available,
            "is_repository": self.is_repository,
            "current_branch": self.current_branch,
            "head_commit": self.head_commit,
            "base_branch": self.base_branch,
            "is_detached_head": self.is_detached_head,
            "working_tree_dirty": self.working_tree_dirty,
            "staged_files_count": len(self.staged_files),
            "modified_files_count": len(self.modified_files),
            "untracked_files_count": len(self.untracked_files),
            "deleted_files_count": len(self.deleted_files),
            "renamed_files_count": len(self.renamed_files),
            "repository_revision": self.repository_revision,
            "detected_at": self.detected_at.isoformat(),
            "warnings": list(self.warnings),
        }

    # ----------------------------------------------------------------
    # Null / degraded factory
    # ----------------------------------------------------------------

    @classmethod
    def unavailable(cls, repository_root: str, reason: str = "") -> "GitContext":
        """Return a safe no-op context when Git is unavailable."""
        warnings = (reason,) if reason else ("Git unavailable; using manifest fallback.",)
        return cls(
            repository_root=repository_root,
            git_available=False,
            is_repository=False,
            warnings=warnings,
        )

    @classmethod
    def non_repository(cls, repository_root: str) -> "GitContext":
        """Return a safe context for a directory that is not a Git repository."""
        return cls(
            repository_root=repository_root,
            git_available=True,
            is_repository=False,
            warnings=("Directory is not a Git repository; using manifest fallback.",),
        )
