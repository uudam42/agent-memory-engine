"""IncrementalIndexCoordinator — scan only added/changed files, skip unchanged.

Stores a JSON file-manifest at .memory-engine/indexes/manifests/manifest.json.

Manifest entry per file:
{
  "rel_path": "src/scheduler/retry.py",
  "content_hash": "sha256...",
  "size": 4096,
  "mtime": 1700000000.0,
  "source_type": "code_file",
  "index_status": "indexed",   // "indexed" | "stale" | "deleted"
  "revision": 1
}

On each startup (or explicit refresh):
  1. List candidate files under project root (respecting include/exclude rules).
  2. For each file: compare against manifest entry.
     - No entry  → NEW  (index it)
     - mtime/size changed → CHANGED  (re-index, mark old chunks stale)
     - Entry matches     → UNCHANGED (skip)
  3. Entries in manifest that have no file → DELETED (mark chunks stale)
  4. Return a ChangeSet summary.

Git optimization:
  If .git exists, optionally use `git diff --name-only` to narrow the scan.
  Falls back to full manifest comparison when Git is unavailable.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from memory_engine.bootstrap.security import is_indexable

# Max files to index in one incremental pass (avoid blocking indefinitely)
MAX_FILES_PER_PASS = 500

# Source type inference by extension
_EXT_SOURCE_TYPE: dict[str, str] = {
    ".md": "markdown",
    ".rst": "markdown",
    ".txt": "manual_note",
    ".py": "code_file",
    ".ts": "code_file",
    ".tsx": "code_file",
    ".js": "code_file",
    ".jsx": "code_file",
    ".go": "code_file",
    ".rs": "code_file",
    ".java": "code_file",
    ".rb": "code_file",
    ".sh": "code_file",
    ".yaml": "api_spec",
    ".yml": "api_spec",
    ".json": "api_spec",
    ".toml": "api_spec",
    ".log": "runtime_log",
    ".diff": "git_diff",
    ".patch": "git_diff",
}


def _infer_source_type(path: Path) -> str:
    name_lower = path.name.lower()
    if name_lower in ("readme.md", "readme.rst", "readme.txt", "readme"):
        return "readme"
    if "adr" in str(path).lower() or name_lower.startswith("adr"):
        return "adr"
    if name_lower in ("architecture.md", "arch.md") or "architecture" in name_lower:
        return "architecture_doc"
    return _EXT_SOURCE_TYPE.get(path.suffix.lower(), "manual_note")


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ManifestEntry:
    rel_path: str
    content_hash: str
    size: int
    mtime: float
    source_type: str
    index_status: str = "indexed"
    revision: int = 0


@dataclass
class ChangeSet:
    new: list[ManifestEntry] = field(default_factory=list)
    changed: list[ManifestEntry] = field(default_factory=list)
    deleted: list[ManifestEntry] = field(default_factory=list)
    unchanged: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.new or self.changed or self.deleted)

    def summary(self) -> dict[str, Any]:
        return {
            "new": len(self.new),
            "changed": len(self.changed),
            "deleted": len(self.deleted),
            "unchanged": self.unchanged,
        }


class IncrementalIndexCoordinator:
    """Track which files need indexing across process restarts."""

    def __init__(self, project_root: Path, manifest_path: Path) -> None:
        self.project_root = project_root.resolve()
        self.manifest_path = manifest_path
        self._manifest: dict[str, ManifestEntry] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Manifest I/O
    # ------------------------------------------------------------------

    def load_manifest(self) -> None:
        if not self.manifest_path.exists():
            self._manifest = {}
            self._loaded = True
            return
        try:
            raw: dict[str, Any] = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
            self._manifest = {k: ManifestEntry(**v) for k, v in raw.items()}
        except Exception:
            self._manifest = {}
        self._loaded = True

    def save_manifest(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in self._manifest.items()}
        self.manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def compute_changes(self, include_patterns: list[str] | None = None) -> ChangeSet:
        """Compare current files against stored manifest.  Returns ChangeSet."""
        if not self._loaded:
            self.load_manifest()

        cs = ChangeSet()
        seen: set[str] = set()

        # Gather candidate files
        candidates = self._gather_files(include_patterns)
        for path in candidates[:MAX_FILES_PER_PASS]:
            indexable, _ = is_indexable(path, self.project_root)
            if not indexable:
                continue

            rel = str(path.relative_to(self.project_root))
            seen.add(rel)
            stat = path.stat()
            existing = self._manifest.get(rel)

            if existing is None:
                # NEW file — hash it
                entry = ManifestEntry(
                    rel_path=rel,
                    content_hash=_file_hash(path),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    source_type=_infer_source_type(path),
                )
                cs.new.append(entry)
            elif (
                existing.index_status == "deleted"
                or abs(existing.mtime - stat.st_mtime) > 1e-3
                or existing.size != stat.st_size
            ):
                # Metadata differs → re-hash to confirm
                new_hash = _file_hash(path)
                if new_hash != existing.content_hash:
                    entry = ManifestEntry(
                        rel_path=rel,
                        content_hash=new_hash,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        source_type=_infer_source_type(path),
                        revision=existing.revision,
                    )
                    cs.changed.append(entry)
                else:
                    # Hash matches — update metadata only
                    existing.mtime = stat.st_mtime
                    existing.size = stat.st_size
                    existing.index_status = "indexed"
                    cs.unchanged += 1
            else:
                cs.unchanged += 1

        # Detect deletions
        for rel, entry in self._manifest.items():
            if rel not in seen and entry.index_status != "deleted":
                cs.deleted.append(entry)

        return cs

    def apply_changes(self, cs: ChangeSet, current_revision: int = 0) -> None:
        """Update manifest to reflect applied ChangeSet."""
        for entry in cs.new:
            entry.index_status = "indexed"
            entry.revision = current_revision
            self._manifest[entry.rel_path] = entry

        for entry in cs.changed:
            entry.index_status = "indexed"
            entry.revision = current_revision
            self._manifest[entry.rel_path] = entry

        for entry in cs.deleted:
            self._manifest[entry.rel_path].index_status = "deleted"

        self.save_manifest()

    def mark_stale(self, rel_path: str) -> None:
        if rel_path in self._manifest:
            self._manifest[rel_path].index_status = "stale"

    # ------------------------------------------------------------------
    # File gathering
    # ------------------------------------------------------------------

    def _gather_files(self, include_patterns: list[str] | None = None) -> list[Path]:
        """Walk project root and collect candidate files."""
        results: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.project_root):
            dp = Path(dirpath)
            # Prune excluded directories in-place
            dirnames[:] = [
                d for d in dirnames
                if not _is_excluded_dir(dp / d, self.project_root)
            ]
            for fname in filenames:
                fpath = dp / fname
                if include_patterns:
                    rel = str(fpath.relative_to(self.project_root))
                    if not any(fnmatch.fnmatch(rel, p) for p in include_patterns):
                        continue
                results.append(fpath)
        return results

    # ------------------------------------------------------------------
    # Git optimization (best-effort)
    # ------------------------------------------------------------------

    def git_changed_files(self) -> list[str] | None:
        """Return list of changed relative paths using git, or None if unavailable."""
        git_dir = self.project_root / ".git"
        if not git_dir.exists():
            return None
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            pass
        return None


def _is_excluded_dir(path: Path, project_root: Path) -> bool:
    from memory_engine.bootstrap.security import is_excluded
    return is_excluded(path, project_root)
