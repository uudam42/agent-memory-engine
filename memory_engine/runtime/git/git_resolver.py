"""GitContextResolver — detect Git state and produce a GitContext snapshot.

Responsibilities:
1. Detect whether the project root is a Git repository.
2. Detect current branch (or detached HEAD state).
3. Detect HEAD commit (short SHA only — never full SHA by default).
4. Detect working tree state: staged, modified, untracked, deleted, renamed.
5. Determine likely base branch and base commit when safe.
6. Return a deterministic GitContext.
7. Degrade gracefully if Git is unavailable or the directory is not a repo.

Security constraints (enforced via git_security module):
- Only safe, read-only Git subcommands are used.
- No remote URLs, user identities, or credentials are collected.
- All commands run inside the resolved project root.
- shell=False always.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memory_engine.runtime.git.git_context import GitContext, GitRename
from memory_engine.runtime.git.git_security import (
    GitSecurityError,
    is_git_available,
    run_git,
)

_LOG = logging.getLogger(__name__)

# Mainline branch names to check for base branch detection
_MAINLINE_CANDIDATES = ("main", "master", "develop", "development", "trunk")

# Maximum number of files to include in each changed-file list
_MAX_FILES_PER_LIST = 200


class GitContextResolver:
    """Resolve Git state for a single project root."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root.resolve()

    def resolve(self) -> GitContext:
        """Return a GitContext snapshot. Never raises; always returns a usable object."""
        root_str = str(self._root)

        if not is_git_available():
            return GitContext.unavailable(root_str, "git binary not found in PATH")

        # Check if inside a work tree
        try:
            stdout, _, rc = run_git(
                ["rev-parse", "--is-inside-work-tree"],
                cwd=self._root,
            )
        except GitSecurityError as exc:
            return GitContext.unavailable(root_str, str(exc))

        if rc != 0 or stdout.strip() != "true":
            return GitContext.non_repository(root_str)

        # Collect everything; each step is individually guarded
        warnings: list[str] = []

        current_branch = self._detect_branch(warnings)
        is_detached = current_branch is None
        head_commit = self._detect_head(warnings)
        staged, modified, untracked, deleted, renamed = self._detect_changes(warnings)
        working_tree_dirty = bool(staged or modified or untracked or deleted or renamed)
        base_branch, base_commit = self._detect_base_branch(current_branch, warnings)

        return GitContext(
            repository_root=root_str,
            git_available=True,
            is_repository=True,
            current_branch=current_branch,
            head_commit=head_commit,
            base_branch=base_branch,
            base_commit=base_commit,
            upstream_branch=None,   # not inspected — avoid remote interaction
            is_detached_head=is_detached,
            working_tree_dirty=working_tree_dirty,
            staged_files=tuple(staged),
            modified_files=tuple(modified),
            untracked_files=tuple(untracked),
            deleted_files=tuple(deleted),
            renamed_files=tuple(renamed),
            repository_revision=head_commit,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------
    # Private detection helpers
    # ------------------------------------------------------------------

    def _detect_branch(self, warnings: list[str]) -> str | None:
        """Return current branch name, or None for detached HEAD."""
        try:
            stdout, _, rc = run_git(
                ["branch", "--show-current"],
                cwd=self._root,
            )
            if rc == 0:
                branch = stdout.strip()
                return branch if branch else None
        except GitSecurityError as exc:
            warnings.append(f"Branch detection failed: {exc}")
        except Exception as exc:
            warnings.append(f"Branch detection error: {exc}")
        return None

    def _detect_head(self, warnings: list[str]) -> str | None:
        """Return short HEAD commit SHA, or None if unavailable."""
        try:
            stdout, _, rc = run_git(
                ["rev-parse", "--short=8", "HEAD"],
                cwd=self._root,
            )
            if rc == 0:
                sha = stdout.strip()
                return sha if sha else None
        except GitSecurityError as exc:
            warnings.append(f"HEAD detection failed: {exc}")
        except Exception as exc:
            warnings.append(f"HEAD detection error: {exc}")
        return None

    def _detect_changes(
        self, warnings: list[str]
    ) -> tuple[list[str], list[str], list[str], list[str], list[GitRename]]:
        """Parse git status --porcelain=v1 for changed file lists.

        Returns: (staged, modified, untracked, deleted, renamed)
        """
        staged: list[str] = []
        modified: list[str] = []
        untracked: list[str] = []
        deleted: list[str] = []
        renamed: list[GitRename] = []

        try:
            stdout, _, rc = run_git(
                ["status", "--porcelain=v1", "-z"],
                cwd=self._root,
            )
            if rc != 0:
                warnings.append("git status returned non-zero; skipping change detection.")
                return staged, modified, untracked, deleted, renamed

            # --porcelain=v1 -z: records are NUL-delimited, not newline-delimited
            # Each record is: "XY path" or for renames "XY new_path\0old_path"
            records = stdout.split("\0")
            idx = 0
            while idx < len(records):
                rec = records[idx]
                if len(rec) < 3:
                    idx += 1
                    continue

                x = rec[0]   # staged status
                y = rec[1]   # unstaged status
                path = rec[3:]

                if x == "R" or y == "R":
                    # Rename: next NUL record is the old path
                    idx += 1
                    old_path = records[idx] if idx < len(records) else ""
                    renamed.append(GitRename(old_path=old_path, new_path=path))
                elif x == "D" or y == "D":
                    deleted.append(path)
                elif x == "?" and y == "?":
                    untracked.append(path)
                elif x != " " and x != "?":
                    staged.append(path)
                    if y not in (" ", "?", "\x00"):
                        modified.append(path)
                elif y not in (" ", "?", "\x00"):
                    modified.append(path)

                idx += 1

        except GitSecurityError as exc:
            warnings.append(f"Change detection failed: {exc}")
        except Exception as exc:
            warnings.append(f"Change detection error: {exc}")

        # Truncate to avoid OOM on massive repos
        return (
            staged[:_MAX_FILES_PER_LIST],
            modified[:_MAX_FILES_PER_LIST],
            untracked[:_MAX_FILES_PER_LIST],
            deleted[:_MAX_FILES_PER_LIST],
            renamed[:_MAX_FILES_PER_LIST],
        )

    def _detect_base_branch(
        self, current_branch: str | None, warnings: list[str]
    ) -> tuple[str | None, str | None]:
        """Determine the likely base branch and merge-base commit.

        Returns (base_branch_name, merge_base_sha) or (None, None).
        Does not fail if no base can be found.
        """
        if not current_branch:
            return None, None
        if current_branch in _MAINLINE_CANDIDATES:
            # On mainline — no distinct base branch
            return None, None

        for candidate in _MAINLINE_CANDIDATES:
            # Check if the candidate branch exists locally
            try:
                stdout, _, rc = run_git(
                    ["rev-parse", "--verify", f"refs/heads/{candidate}"],
                    cwd=self._root,
                )
                if rc != 0:
                    continue

                # Candidate exists — compute merge-base
                stdout, _, rc = run_git(
                    ["merge-base", "HEAD", candidate],
                    cwd=self._root,
                )
                if rc == 0 and stdout.strip():
                    merge_base = stdout.strip()[:8]  # short SHA only
                    return candidate, merge_base
            except Exception:
                pass

        warnings.append(
            "Could not determine base branch; no known mainline branch found locally."
        )
        return None, None
