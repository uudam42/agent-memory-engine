"""ProjectRootResolver — safely determine the target project root.

Priority order:
  1. Explicit --project-root argument (or MEMORY_ENGINE_PROJECT_ROOT env var)
  2. MCP client working directory (passed as cwd_hint)
  3. Current process working directory
  4. Walk upward from cwd for repository markers
  5. Structured error if nothing found

Safety rules:
  - Never follow symlinks outside the resolved root.
  - Normalize all paths.
  - Never accidentally return the Memory Engine runtime repo itself
    unless it is the same as the target project.
  - Always return an absolute path.
"""

from __future__ import annotations

import os
from pathlib import Path

from memory_engine.bootstrap.security import PathEscapeError, resolve_safe

# Markers that indicate a repository root when walking upward
_REPO_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "setup.py",
    "setup.cfg",
    "CMakeLists.txt",
    "Makefile",
)

# The Memory Engine runtime package directory
_ME_PACKAGE_ROOT = Path(__file__).parent.parent.parent.resolve()


class ProjectRootError(Exception):
    """Raised when project root cannot be safely resolved."""


def _walk_for_marker(start: Path) -> Path | None:
    """Walk upward from start looking for a repository marker.

    Returns the first directory that contains a marker, or None.
    Does not cross filesystem boundaries (best-effort).
    """
    current = start.resolve()
    visited: set[Path] = set()

    while current != current.parent:
        if current in visited:
            break
        visited.add(current)
        for marker in _REPO_MARKERS:
            if (current / marker).exists():
                return current
        current = current.parent

    return None


def resolve_project_root(
    explicit: str | None = None,
    cwd_hint: str | None = None,
) -> Path:
    """Resolve and validate the target project root.

    Parameters
    ----------
    explicit:
        Value from --project-root CLI arg or MEMORY_ENGINE_PROJECT_ROOT env var.
    cwd_hint:
        Working directory reported by the MCP client (if any).

    Returns
    -------
    Path
        Absolute, resolved project root directory.

    Raises
    ------
    ProjectRootError
        If resolution fails or the result is unsafe.
    """
    # 1. Explicit argument or env var
    if explicit is None:
        explicit = os.environ.get("MEMORY_ENGINE_PROJECT_ROOT")

    if explicit:
        candidate = Path(explicit).resolve()
        _validate_root(candidate)
        return candidate

    # 2. MCP client working directory
    if cwd_hint:
        candidate = Path(cwd_hint).resolve()
        if candidate.is_dir():
            _validate_root(candidate)
            return candidate

    # 3. Current process working directory
    cwd = Path.cwd().resolve()

    # 4. Walk upward for repo markers
    marker_root = _walk_for_marker(cwd)
    if marker_root and marker_root.is_dir():
        _validate_root(marker_root)
        return marker_root

    # 5. Fall back to cwd itself
    _validate_root(cwd)
    return cwd


def _validate_root(path: Path) -> None:
    """Raise ProjectRootError for unsafe or non-existent roots."""
    if not path.exists():
        raise ProjectRootError(f"Project root does not exist: {path}")
    if not path.is_dir():
        raise ProjectRootError(f"Project root is not a directory: {path}")

    # Null-byte guard
    if "\0" in str(path):
        raise ProjectRootError("Project root path contains null bytes")

    # Resolve to catch symlink tricks
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise ProjectRootError(f"Cannot resolve project root: {exc}") from exc

    if not resolved.is_dir():
        raise ProjectRootError(f"Resolved project root is not a directory: {resolved}")


def is_memory_engine_repo(path: Path) -> bool:
    """Return True if path looks like the Memory Engine runtime repository itself."""
    return path.resolve() == _ME_PACKAGE_ROOT


def safe_relative(path: Path, root: Path) -> str:
    """Return a safe relative path string, or raise PathEscapeError."""
    resolved = resolve_safe(path, root)
    root_resolved = root.resolve()
    return str(resolved.relative_to(root_resolved))
