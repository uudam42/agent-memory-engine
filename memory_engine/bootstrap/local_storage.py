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

    def bind_fingerprint(self) -> None:
        """Write the canonical project root path as the fingerprint.

        Called on first initialization. Idempotent if the fingerprint already
        matches; raises ProjectRootMismatchError if it conflicts.
        """
        canonical = str(self.project_root)
        if self.fingerprint_path.exists():
            self.verify_project_fingerprint()
        else:
            self.fingerprint_path.write_text(canonical, encoding="utf-8")

    def verify_project_fingerprint(self) -> None:
        """Raise ProjectRootMismatchError if the stored fingerprint does not
        match the current project root.

        This prevents a copied .memory-engine/ from silently inheriting
        another project's memories.  No-ops if no fingerprint exists yet
        (pre-fingerprint databases are treated as unbound).
        """
        if not self.fingerprint_path.exists():
            return  # unbound — skip check; bind_fingerprint() will write it
        stored = self.fingerprint_path.read_text(encoding="utf-8").strip()
        current = str(self.project_root)
        if stored != current:
            raise ProjectRootMismatchError(
                f"Memory Engine refuses to open: .memory-engine/ was created for "
                f"'{stored}' but is being accessed from '{current}'. "
                "Delete .memory-engine/ or re-run 'memory init' in this directory."
            )

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
