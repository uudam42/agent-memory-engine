"""MCP-layer error types."""

from __future__ import annotations


class MCPProjectError(Exception):
    """Raised when the target project cannot be safely resolved or bootstrapped."""


class MCPBoundaryError(Exception):
    """Raised when a tool attempts to access a path outside the project root."""


class MCPDegradedError(Exception):
    """Raised in FAILED bootstrap state when retrieval is impossible."""
