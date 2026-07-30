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
  needs_revalidation  -> source file exists but content hash (or symbol, or
                         commit reachability) indicates drift
  invalidated         -> source file no longer exists at the recorded path

Hash semantics (Task 3):
  ``source_hash`` is a **full-file content hash** (sha256 of raw bytes),
  computed once at write time and compared byte-for-byte on each check.
  This is coarse: an edit to *any* part of the file (including an unrelated
  docstring or an unrelated function) changes the hash and can trigger
  ``needs_revalidation`` even though the specific fact the memory describes
  is untouched. A symbol-range hash or normalized-text hash would be more
  precise, but this project has no existing symbol-range index or AST layer
  to derive one safely — see ``symbol_exists_in_file`` below for the (much
  more limited) symbol-presence check that partially compensates for this.
  Full-file hashing was chosen as the least-disruptive option consistent
  with the existing indexing model (chunk-level, not symbol-range).

Symbol validation semantics (Task 8):
  When a node also carries ``source_symbol``, and the source language is one
  this module knows how to scan cheaply (currently: Python, via a regex over
  ``def``/``class`` declarations — not a full AST), a missing symbol produces
  ``needs_revalidation`` (never immediate deletion/invalidation — the file
  may simply have been reformatted, or the symbol renamed by a refactor).
  For any other language, symbol validation is skipped entirely (documented,
  not faked) and the result falls back to file-level (existence/hash) only.

Commit reachability semantics (Task 9):
  When a node carries ``commit_sha`` and the project is a Git repository,
  a lightweight, read-only reachability check (`git merge-base
  --is-ancestor`) determines whether that commit is still reachable from
  HEAD. Unreachable commits produce ``needs_revalidation`` (the branch the
  memory was written on may have been rebased or deleted — not proof the
  fact is wrong). Any Git error, missing repository, or malformed SHA
  produces "unknown" and is treated as a no-op — this service never
  fabricates validity from an errored Git call.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from memory_engine.bootstrap.security import PathEscapeError, resolve_safe
from memory_engine.models.domain import MemoryNode, MemoryStatus

# Statuses a validity check must never override — the node has already left
# the "authoritative or needs-checking" lifecycle for an unrelated reason.
_TERMINAL_STATUSES = frozenset({
    MemoryStatus.superseded,
    MemoryStatus.archived,
})

# Bound how much of a file we hash in one read (Task 3: bounded reads).
# Streamed in fixed-size chunks so memory use stays flat regardless of file size.
_HASH_CHUNK_BYTES = 1 << 20  # 1 MiB per read() call

# Commit SHAs we consider well-formed enough to attempt a reachability check.
# Anything else (empty string, obviously-truncated legacy values, etc.) is
# treated as "unknown" rather than risking a malformed shell/subprocess arg.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class ValidityCheck:
    """Result of checking one node's source evidence."""

    changed: bool
    new_status: MemoryStatus | None = None
    reason: str | None = None
    new_source_hash: str | None = None


def hash_file(path: Path) -> str | None:
    """sha256 hex digest of a file's bytes, or None if it cannot be read.

    Streams the file in bounded chunks (Task 3) rather than reading it
    entirely into memory, so this is safe to call on large files.
    """
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def compute_source_hash(project_root: Path, source_path: str) -> str | None:
    """Deterministic sha256 of a project-relative file's contents.

    Safety (Task 3):
      - Resolves through ``resolve_safe`` so the path can never escape
        ``project_root`` (blocks path traversal and symlink escape to
        outside-the-project targets).
      - Returns None (never fabricates a hash) for: missing files,
        directories, unreadable files, or paths that fail the safety check.
    """
    try:
        resolved = resolve_safe(source_path, project_root)
    except PathEscapeError:
        return None
    if not resolved.is_file():
        return None
    return hash_file(resolved)


# ---------------------------------------------------------------------------
# Task 8 — minimum symbol-aware validation
# ---------------------------------------------------------------------------

# Extensions this module can cheaply scan for a top-level def/class symbol.
# Anything else falls back to file-level validation only (documented
# limitation — see module docstring). This is intentionally NOT a general
# multi-language AST framework.
_PY_EXTENSIONS = frozenset({".py", ".pyi"})


def symbol_exists_in_file(path: Path, symbol: str) -> bool | None:
    """Best-effort check that ``symbol`` (a bare name) is still declared in ``path``.

    Returns:
      True  — a ``def``/``class``/``async def`` declaration for the symbol was found.
      False — the file was readable but no such declaration was found.
      None  — unsupported language, or the file could not be read (unknown;
              callers must treat this as "do not touch", never as False).

    This is a lightweight regex scan, not an AST parse: it will not catch a
    symbol accessed only as ``self.symbol`` or reachable through re-exports,
    and it does not resolve nested classes/methods by qualified name. It errs
    towards False negatives being safe (produces needs_revalidation, which is
    reversible) rather than silently trusting stale symbol evidence forever.
    """
    if path.suffix not in _PY_EXTENSIONS:
        return None  # unsupported language — documented limitation, not faked
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    escaped = re.escape(symbol)
    pattern = re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{escaped}\s*[\(:]",
        re.MULTILINE,
    )
    return bool(pattern.search(text))


# ---------------------------------------------------------------------------
# Task 9 — minimum commit reachability validation
# ---------------------------------------------------------------------------


def _is_git_repository(project_root: Path) -> bool:
    return (project_root / ".git").exists()


def check_commit_reachable(project_root: Path, commit: str) -> bool | None:
    """Return True/False/None (unknown) for whether ``commit`` is reachable from HEAD.

    Read-only, uses only allowed Git subcommands (rev-parse, merge-base).
    Never raises — any Git error, missing repository, or malformed SHA
    produces None (unknown), which callers must treat conservatively
    (no-op, never fabricated validity).
    """
    if not commit or not _COMMIT_SHA_RE.match(commit):
        return None
    if not _is_git_repository(project_root):
        return None

    try:
        from memory_engine.runtime.git.git_security import run_git
    except Exception:
        return None

    try:
        # First confirm the commit object actually exists locally — merge-base
        # --is-ancestor exits 1 both for "not an ancestor" and for "unknown
        # revision", and we must not conflate the two (Task 9 requirement).
        _out, _err, code = run_git(
            ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
            cwd=project_root,
        )
        if code != 0:
            return None  # commit not found locally — unknown, not "unreachable"

        _out, _err, code = run_git(
            ["merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=project_root,
        )
        if code == 0:
            return True
        if code == 1:
            return False
        return None
    except Exception:
        # Never let a Git error crash retrieval (Task 9 requirement).
        return None


class SourceValidityService:
    """Conservative, lazy source-validity checker.

    Constructed per request/recall call. Carries a small in-instance cache
    for Git commit-reachability lookups (Task 9: "cache reachability checks
    where practical") — bounded to the lifetime of one RecallService call,
    never shared or persisted across requests.
    """

    def __init__(self) -> None:
        self._commit_cache: dict[tuple[str, str], bool | None] = {}

    def _resolve(self, project_root: Path, source_path: str) -> Path:
        p = Path(source_path)
        if p.is_absolute():
            return p
        return project_root / p

    def _cached_commit_reachable(self, project_root: Path, commit: str) -> bool | None:
        key = (str(project_root), commit)
        if key not in self._commit_cache:
            self._commit_cache[key] = check_commit_reachable(project_root, commit)
        return self._commit_cache[key]

    @staticmethod
    def _hash_with_cache(
        resolved: Path, hash_cache: dict[str, str | None] | None
    ) -> str | None:
        if hash_cache is None:
            return hash_file(resolved)
        key = str(resolved)
        if key not in hash_cache:
            hash_cache[key] = hash_file(resolved)
        return hash_cache[key]

    def check(
        self,
        node: MemoryNode,
        project_root: Path,
        hash_cache: dict[str, str | None] | None = None,
    ) -> ValidityCheck:
        """Decide whether ``node`` should transition to a different status.

        Returns a no-op ValidityCheck (changed=False) when:
          - the node has no source_path (nothing to validate — legacy memory);
          - the node is already in a terminal status (superseded/archived);
          - the source file still exists and its hash/symbol/commit evidence
            (whichever is present) is unchanged or cannot be determined.

        ``hash_cache`` (Phase 3A review A8): an optional caller-owned dict,
        shared across every ``check()`` call within one recall/revalidation
        request, that memoizes ``hash_file()`` by resolved path. Several
        nodes can legitimately point at the same source file (e.g. a module
        summary and a debug/incident note derived from the same edit); this
        avoids re-reading and re-hashing that file once per node within a
        single request. Never shared or persisted across requests.
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

        # File exists. If we have a recorded hash, compare content drift.
        if node.source_hash:
            current_hash = self._hash_with_cache(resolved, hash_cache)
            if current_hash is not None and current_hash != node.source_hash:
                if node.status != MemoryStatus.needs_revalidation:
                    return ValidityCheck(
                        changed=True,
                        new_status=MemoryStatus.needs_revalidation,
                        reason=f"source content changed since memory was written: {node.source_path}",
                    )
                return ValidityCheck(changed=False)

        # Task 8: symbol-level check (only runs when file/hash checks above
        # did not already flag drift, and only for a language this module
        # can scan — see symbol_exists_in_file docstring).
        if node.source_symbol:
            exists = symbol_exists_in_file(resolved, node.source_symbol)
            if exists is False and node.status != MemoryStatus.needs_revalidation:
                return ValidityCheck(
                    changed=True,
                    new_status=MemoryStatus.needs_revalidation,
                    reason=(
                        f"source symbol '{node.source_symbol}' no longer found "
                        f"in {node.source_path}"
                    ),
                )

        # Task 9: commit reachability (only runs when nothing above already
        # flagged drift — file/symbol evidence is more specific).
        if node.commit_sha:
            reachable = self._cached_commit_reachable(project_root, node.commit_sha)
            if reachable is False and node.status != MemoryStatus.needs_revalidation:
                return ValidityCheck(
                    changed=True,
                    new_status=MemoryStatus.needs_revalidation,
                    reason=f"source commit {node.commit_sha} is not reachable from HEAD",
                )

        return ValidityCheck(changed=False)

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
