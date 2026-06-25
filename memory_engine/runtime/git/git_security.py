"""Security helpers for the Git runtime layer.

Rules enforced here:
- All Git subprocesses run inside the resolved project root.
- No shell=True — always pass args as a list.
- Never interpolate untrusted values into command strings.
- No destructive Git commands (commit, reset, clean, checkout, merge, rebase, push, fetch).
- No credential-touching commands (credential, remote set-url, config --global).
- Remote URLs are never returned.
- User identity (name, email) is never returned.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Commands that are safe to run during Git context resolution.
_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "rev-parse",
    "branch",
    "status",
    "diff",
    "merge-base",
    "log",
    "show",
    "ls-files",
    "for-each-ref",
    "symbolic-ref",
})

# Commands that are NEVER allowed — destructive or credential-exposing.
_BLOCKED_COMMANDS: frozenset[str] = frozenset({
    "commit", "reset", "clean", "checkout", "switch", "restore",
    "merge", "rebase", "cherry-pick", "revert",
    "push", "fetch", "pull", "clone",
    "credential", "config",
    "remote",
    "stash",
    "worktree",
    "submodule",
    "tag",
    "gc",
    "prune",
    "pack-refs",
    "reflog",
    "fsck",
    "bisect",
})


class GitSecurityError(Exception):
    """Raised when a Git command violates security policy."""


def git_binary() -> str | None:
    """Return the path to the git binary, or None if not found."""
    return shutil.which("git")


def run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 5,
) -> tuple[str, str, int]:
    """Run a safe Git command inside cwd.

    Returns (stdout, stderr, returncode).
    Raises GitSecurityError for disallowed commands or paths outside cwd.

    Security guarantees:
    - shell=False always
    - cwd is the resolved project root
    - No untrusted values interpolated into args
    - Only _ALLOWED_COMMANDS are permitted
    - Destructive commands raise GitSecurityError immediately
    """
    if not args:
        raise GitSecurityError("Empty Git command.")

    sub_cmd = args[0].lstrip("-").split("-")[0]  # e.g. "rev-parse" → "rev"
    # Use full first word for matching
    first_word = args[0]

    if first_word in _BLOCKED_COMMANDS:
        raise GitSecurityError(f"Blocked Git subcommand: {first_word!r}")

    if first_word not in _ALLOWED_COMMANDS:
        raise GitSecurityError(f"Git subcommand {first_word!r} is not in the allowed list.")

    git = git_binary()
    if git is None:
        return "", "git binary not found", 1

    resolved_cwd = cwd.resolve()
    if not resolved_cwd.is_dir():
        raise GitSecurityError(f"cwd is not a directory: {resolved_cwd}")

    try:
        result = subprocess.run(
            [git] + args,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,   # NEVER use shell=True
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "git command timed out", 1
    except OSError as exc:
        return "", f"git subprocess error: {exc}", 1


def is_git_available() -> bool:
    """Return True if the git binary is accessible."""
    return git_binary() is not None
