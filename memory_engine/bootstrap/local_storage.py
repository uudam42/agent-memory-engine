"""ProjectLocalStorage — manage the .memory-engine/ directory inside a target project.

Layout created by ensure_layout():

<project-root>/
└── .memory-engine/
    ├── config.yaml            (user-editable; preserved across runs)
    ├── project_state.json     (machine-written; revision tracking)
    ├── memory.db              (SQLite: memory nodes, knowledge, FTS5)
    ├── indexes/
    │   ├── lexical/           (future: separate FTS databases if needed)
    │   ├── vector/            (future: persistent vector files)
    │   └── manifests/
    │       └── manifest.json  (incremental-index file manifest)
    ├── cache/                 (future: disk cache)
    ├── logs/
    ├── generated/
    │   └── AGENT_MEMORY_POLICY.md
    └── bootstrap/
        └── bootstrap_report.json

Rules:
  - The .memory-engine/ directory is created atomically on first use.
  - Deleting it fully removes all local Memory Engine state.
  - A recommended .gitignore block is provided as a helper.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


STORAGE_DIR_NAME = ".memory-engine"

_GITIGNORE_BLOCK = """\
# Memory Engine local state — do not commit
.memory-engine/memory.db
.memory-engine/memory.db-shm
.memory-engine/memory.db-wal
.memory-engine/indexes/
.memory-engine/cache/
.memory-engine/logs/
.memory-engine/generated/
.memory-engine/bootstrap/
.memory-engine/project_state.json

# Optionally commit these (human-authored):
# .memory-engine/config.yaml
# .memory-engine/constraints.md
# .memory-engine/team-rules.md
# .memory-engine/decisions.md
"""


class ProjectLocalStorage:
    """Manages the .memory-engine/ directory for a single target project."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.storage_dir = self.project_root / STORAGE_DIR_NAME

    # ------------------------------------------------------------------
    # Path accessors (all return Path objects)
    # ------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.storage_dir / "config.yaml"

    @property
    def project_state_path(self) -> Path:
        return self.storage_dir / "project_state.json"

    @property
    def db_path(self) -> Path:
        return self.storage_dir / "memory.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def vector_db_path(self) -> Path:
        """Phase 13: persistent sqlite-vec vector store (separate from memory.db)."""
        return self.storage_dir / "vector.db"

    @property
    def indexes_dir(self) -> Path:
        return self.storage_dir / "indexes"

    @property
    def manifests_dir(self) -> Path:
        return self.storage_dir / "indexes" / "manifests"

    @property
    def manifest_path(self) -> Path:
        return self.manifests_dir / "manifest.json"

    @property
    def cache_dir(self) -> Path:
        return self.storage_dir / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.storage_dir / "logs"

    @property
    def generated_dir(self) -> Path:
        return self.storage_dir / "generated"

    @property
    def bootstrap_dir(self) -> Path:
        return self.storage_dir / "bootstrap"

    @property
    def bootstrap_report_path(self) -> Path:
        return self.bootstrap_dir / "bootstrap_report.json"

    @property
    def agent_policy_path(self) -> Path:
        return self.generated_dir / "AGENT_MEMORY_POLICY.md"

    # Seed files the user may create
    @property
    def constraints_path(self) -> Path:
        return self.storage_dir / "constraints.md"

    @property
    def team_rules_path(self) -> Path:
        return self.storage_dir / "team-rules.md"

    @property
    def decisions_path(self) -> Path:
        return self.storage_dir / "decisions.md"

    # ------------------------------------------------------------------
    # Layout initialisation
    # ------------------------------------------------------------------

    def ensure_layout(self) -> None:
        """Create the .memory-engine/ directory tree if missing.

        Safe to call multiple times (idempotent).
        """
        for d in (
            self.storage_dir,
            self.indexes_dir,
            self.indexes_dir / "lexical",
            self.indexes_dir / "vector",
            self.manifests_dir,
            self.cache_dir,
            self.logs_dir,
            self.generated_dir,
            self.bootstrap_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def fingerprint_path(self) -> Path:
        """File that records the canonical project root path on first bind."""
        return self.storage_dir / "project.fingerprint"

    # ------------------------------------------------------------------
    # Phase 14: robust multi-field fingerprint
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_remote_url(url: str) -> str:
        """Normalize git remote URL to a canonical form.

        Maps equivalent SSH and HTTPS remotes to the same string so that
        switching from SSH to HTTPS does not change the fingerprint.

        Examples:
          git@github.com:owner/repo.git  → github.com/owner/repo
          https://github.com/owner/repo.git → github.com/owner/repo
        """
        import re
        # SSH: git@host:path[.git]
        m = re.match(r'^git@([^:]+):(.+?)(?:\.git)?$', url.strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        # HTTPS: https://host/path[.git]
        m = re.match(r'^https?://([^/]+)/(.+?)(?:\.git)?$', url.strip())
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        # Fallback: use as-is (strip trailing .git)
        return re.sub(r'\.git$', '', url.strip())

    @staticmethod
    def _get_git_remote_hash(project_root: Path) -> str | None:
        """Run git remote get-url origin, normalize, and return sha256 hex.

        Returns None if git is unavailable or no remote is configured.
        The raw URL is never stored — only its hash is retained.
        """
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            normalized = ProjectLocalStorage._normalize_remote_url(result.stdout.strip())
            return hashlib.sha256(normalized.encode()).hexdigest()[:32]
        except Exception:
            return None

    @staticmethod
    def _get_git_common_dir(project_root: Path) -> str | None:
        """Return the git common directory (handles worktrees correctly)."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                common = Path(result.stdout.strip()).resolve()
                return str(common)
        except Exception:
            pass
        return None

    def short_repository_fingerprint(self) -> str:
        """Issue 6 — compact, non-identifying project marker for provenance
        envelopes returned to MCP clients (once per response, never repeated
        per retrieved memory).

        Derived from the already-computed ``path_hash`` (itself a sha256 of
        ``canonical_path`` — never the raw path — see ``_build_fingerprint``)
        truncated to 12 hex characters. Never derived from
        ``remote_url_hash`` or ``canonical_path`` directly, consistent with
        the privacy rules already established for the Phase 14 fingerprint
        file itself: no raw absolute path and no raw remote URL ever leave
        this method.
        """
        return self._build_fingerprint()["path_hash"][:12]

    def _build_fingerprint(self) -> dict:
        """Build the fingerprint payload for this project root.

        Returns a dict with:
          version: int (1)
          canonical_path: str  — resolved absolute path
          remote_url_hash: str | None  — sha256 of normalized remote URL (no raw URL)
          git_common_dir: str | None  — resolved git common dir (worktree-aware)
          path_hash: str  — sha256 of canonical_path
        """
        canonical = str(self.project_root)
        remote_hash = self._get_git_remote_hash(self.project_root)
        common_dir = self._get_git_common_dir(self.project_root)
        return {
            "version": 1,
            "canonical_path": canonical,
            "remote_url_hash": remote_hash,
            "git_common_dir": common_dir,
            "path_hash": hashlib.sha256(canonical.encode()).hexdigest()[:32],
        }

    def _load_stored_fingerprint(self) -> dict | None:
        """Read and parse the stored fingerprint file.

        Handles both legacy format (plain canonical path) and v1 JSON format.
        Returns None if the file does not exist.
        """
        if not self.fingerprint_path.exists():
            return None
        raw = self.fingerprint_path.read_text(encoding="utf-8").strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        # Legacy v0: plain canonical path
        return {"version": 0, "canonical_path": raw}

    def bind_fingerprint(self) -> None:
        """Write or verify the project fingerprint.

        Called on first initialization. Idempotent if the fingerprint already
        matches; raises ProjectRootMismatchError if it conflicts.
        """
        if self.fingerprint_path.exists():
            self.verify_project_fingerprint()
        else:
            fp = self._build_fingerprint()
            self.fingerprint_path.write_text(
                json.dumps(fp, indent=2), encoding="utf-8"
            )

    def verify_project_fingerprint(self) -> None:
        """Raise ProjectRootMismatchError if the stored fingerprint does not
        match the current project root.

        Matching rules (all must pass when present):
          1. canonical_path must match (always checked)
          2. git_common_dir must match if both stored and current are non-None
             (this catches copies between repositories sharing no git history)

        remote_url_hash is intentionally NOT used as a blocking condition because
        a cloned copy of the same remote is a legitimate database migration path.
        It can be used by callers for advisory comparison.

        No-ops if no fingerprint exists yet (pre-fingerprint databases are unbound).
        """
        stored = self._load_stored_fingerprint()
        if stored is None:
            return  # unbound — bind_fingerprint() will write it

        current_path = str(self.project_root)
        stored_path = stored.get("canonical_path", "")

        if stored_path and stored_path != current_path:
            raise ProjectRootMismatchError(
                f"Memory Engine refuses to open: .memory-engine/ was created for "
                f"'{stored_path}' but is being accessed from '{current_path}'. "
                "Delete .memory-engine/ or re-run 'memory init' in this directory."
            )

        # Additional check: git common directory (catches copy-to-different-worktree-root)
        if stored.get("version", 0) >= 1:
            stored_common = stored.get("git_common_dir")
            current_common = self._get_git_common_dir(self.project_root)
            if stored_common and current_common and stored_common != current_common:
                raise ProjectRootMismatchError(
                    f"Memory Engine refuses to open: .memory-engine/ git common directory "
                    f"'{stored_common}' does not match current '{current_common}'. "
                    "This database appears to belong to a different repository."
                )

    def get_fingerprint_dict(self) -> dict | None:
        """Return the stored fingerprint as a dict, or None if not yet bound."""
        return self._load_stored_fingerprint()

    def is_initialized(self) -> bool:
        """Return True if the storage directory exists and has a DB."""
        return self.storage_dir.exists() and self.db_path.exists()

    # ------------------------------------------------------------------
    # .gitignore helper
    # ------------------------------------------------------------------

    def gitignore_block(self) -> str:
        """Return a recommended .gitignore block for the project."""
        return _GITIGNORE_BLOCK

    def ensure_gitignore_hint(self) -> None:
        """Write a .gitignore-hint file so users know what to add."""
        hint_path = self.storage_dir / ".gitignore-hint"
        if not hint_path.exists():
            hint_path.write_text(_GITIGNORE_BLOCK, encoding="utf-8")


class ProjectRootMismatchError(RuntimeError):
    """Raised when .memory-engine/ fingerprint does not match the current project root."""
