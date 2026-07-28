"""MCP-layer error types."""

from __future__ import annotations


class MCPProjectError(Exception):
    """Raised when the target project cannot be safely resolved or bootstrapped."""


class MCPBoundaryError(Exception):
    """Raised when a tool attempts to access a path outside the project root."""


class MCPDegradedError(Exception):
    """Raised in FAILED bootstrap state when retrieval is impossible."""


# ---------------------------------------------------------------------------
# Phase 14: workspace isolation errors
# ---------------------------------------------------------------------------


class MCPWorkspaceMismatchError(Exception):
    """Raised when the caller's workspace_root does not match the MCP server's project.

    Machine-readable error codes:
      PROJECT_CONTEXT_MISMATCH         — workspace_root doesn't match server project root
      REPOSITORY_FINGERPRINT_MISMATCH  — fingerprint hash doesn't match stored fingerprint
      CURRENT_FILE_OUTSIDE_PROJECT     — a current_file path is outside project root
      PROJECT_CONTEXT_UNVERIFIABLE     — strict mode but no workspace context provided
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")
