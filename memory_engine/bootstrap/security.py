"""Path safety and boundary enforcement for Phase 7.

Rules enforced here:
  - All accessed paths must be inside the resolved project root.
  - Symlinks that escape the project root are rejected.
  - Path traversal (../../) is blocked.
  - Binary files are detected and skipped.
  - Files above the size limit are skipped.
  - Default exclude patterns are applied.

These checks are called by ProjectRootResolver, ProjectBootstrapService,
and IncrementalIndexCoordinator before any file is read or indexed.
"""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

# Glob-style patterns that are always excluded.
# Applied as prefix-path or stem checks, not full glob expansion,
# for performance.  Paths that match any pattern are silently skipped.
DEFAULT_EXCLUDE_PREFIXES: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    "coverage",
    ".coverage",
    "vendor",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    "*.egg-info",
    ".memory-engine",   # never index our own storage
})

DEFAULT_EXCLUDE_FILENAMES: frozenset[str] = frozenset({
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".DS_Store",
    "Thumbs.db",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "secrets.json",
    "credentials.json",
    "service_account.json",
})

DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".aws",
    ".gcp",
    ".azure",
    "secrets",
    "private",
    ".ssh",
    "credentials",
})

# Text MIME type prefixes
_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/yaml",
                        "application/toml", "application/xml")

# Binary extension quick-list (supplement MIME detection)
_BINARY_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".a", ".lib",
    ".o", ".obj", ".class", ".jar", ".war", ".ear",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav",
    ".ttf", ".otf", ".woff", ".woff2",
    ".db", ".sqlite", ".sqlite3",
    ".lock",        # yarn.lock / package-lock.json are large but text — handled by size
})


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class PathEscapeError(Exception):
    """Raised when a path escapes the project root boundary."""


def resolve_safe(path: Path | str, project_root: Path) -> Path:
    """Resolve *path* and verify it sits inside *project_root*.

    Raises PathEscapeError if:
      - The resolved path escapes project_root (path traversal / symlink escape).
      - The path component contains null bytes.
    """
    raw = str(path)
    if "\0" in raw:
        raise PathEscapeError(f"Null byte in path: {raw!r}")

    target = Path(raw)
    if not target.is_absolute():
        target = project_root / target

    # Resolve symlinks — this is where symlink escape would be caught
    try:
        resolved = target.resolve()
        root_resolved = project_root.resolve()
    except OSError as exc:
        raise PathEscapeError(f"Cannot resolve path {raw!r}: {exc}") from exc

    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise PathEscapeError(
            f"Path {resolved!r} escapes project root {root_resolved!r}"
        )

    return resolved


def is_safe_symlink(path: Path, project_root: Path) -> bool:
    """Return True only if path is a symlink whose target is inside project_root."""
    if not path.is_symlink():
        return True   # not a symlink — always ok
    try:
        resolve_safe(path, project_root)
        return True
    except PathEscapeError:
        return False


# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------


def _matches_exclude(rel_parts: tuple[str, ...]) -> bool:
    """Return True if any path component matches an exclude pattern."""
    for part in rel_parts:
        # Direct prefix match
        if part in DEFAULT_EXCLUDE_PREFIXES:
            return True
        # Glob-style *.egg-info
        if part.endswith(".egg-info"):
            return True
        # Filenames
        if part in DEFAULT_EXCLUDE_FILENAMES:
            return True
        if part in DEFAULT_EXCLUDE_DIRS:
            return True
        # Credential / private key filenames
        stem, ext = os.path.splitext(part)
        if ext.lower() in (".pem", ".key", ".p12", ".pfx"):
            return True
        if stem.lower() in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"):
            return True
    return False


def is_excluded(path: Path, project_root: Path) -> bool:
    """Return True if path should be excluded from indexing."""
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return True  # outside root → always excluded

    parts = rel.parts
    return _matches_exclude(parts)


def is_binary(path: Path) -> bool:
    """Return True if the file is likely binary and should not be indexed."""
    suffix = path.suffix.lower()
    if suffix in _BINARY_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        # Read first 512 bytes and check for null bytes
        try:
            with open(path, "rb") as f:
                chunk = f.read(512)
            return b"\x00" in chunk
        except OSError:
            return True
    return not any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES)


def is_oversized(path: Path, max_bytes: int = MAX_FILE_SIZE_BYTES) -> bool:
    """Return True if the file exceeds the size limit."""
    try:
        return path.stat().st_size > max_bytes
    except OSError:
        return True


def is_indexable(path: Path, project_root: Path) -> tuple[bool, str]:
    """Return (can_index, reason) for a given file path."""
    if not path.is_file():
        return False, "not_a_file"
    if is_excluded(path, project_root):
        return False, "excluded_by_rules"
    if not is_safe_symlink(path, project_root):
        return False, "unsafe_symlink"
    if is_binary(path):
        return False, "binary_file"
    if is_oversized(path):
        return False, "file_too_large"
    return True, "ok"
