"""ProjectState — revision tracking persisted to project_state.json.

Fields:
  project_revision    git SHA or content-manifest hash
  index_revision      increments each time indexing completes
  memory_revision     increments on promotion / stale / supersede
  knowledge_revision  increments on document ingest / update / delete
  bootstrap_status    UNINITIALIZED | BOOTSTRAPPING | READY | INDEXING_INCREMENTAL | DEGRADED | FAILED
  indexed_documents   count of indexed KnowledgeDocuments
  indexed_chunks      count of indexed KnowledgeChunks
  active_memories     count of active MemoryNodes
  last_indexed_at     ISO-8601 timestamp
  warnings            list of non-fatal warnings
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


BootstrapStatus = Literal[
    "UNINITIALIZED",
    "BOOTSTRAPPING",
    "READY",
    "INDEXING_INCREMENTAL",
    "DEGRADED",
    "FAILED",
]


class ProjectState(BaseModel):
    project_revision: str = ""
    index_revision: int = 0
    memory_revision: int = 0
    knowledge_revision: int = 0
    bootstrap_status: BootstrapStatus = "UNINITIALIZED"
    indexed_documents: int = 0
    indexed_chunks: int = 0
    active_memories: int = 0
    last_indexed_at: str = ""
    warnings: list[str] = Field(default_factory=list)

    def bump_memory(self) -> None:
        self.memory_revision += 1

    def bump_knowledge(self) -> None:
        self.knowledge_revision += 1
        self.index_revision += 1
        self.last_indexed_at = _now_iso()

    def bump_index(self) -> None:
        self.index_revision += 1
        self.last_indexed_at = _now_iso()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStateManager:
    """Load, update, and persist ProjectState for a single project."""

    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._state: ProjectState | None = None

    def load(self) -> ProjectState:
        if self._state is not None:
            return self._state

        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._state = ProjectState.model_validate(data)
                return self._state
            except Exception:
                pass  # Corrupt state → start fresh

        self._state = ProjectState()
        return self._state

    def save(self) -> None:
        if self._state is None:
            return
        self._path.write_text(
            self._state.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def update(self, **kwargs) -> ProjectState:  # type: ignore[type-arg]
        state = self.load()
        for k, v in kwargs.items():
            setattr(state, k, v)
        self.save()
        return state

    def set_status(self, status: BootstrapStatus) -> ProjectState:
        return self.update(bootstrap_status=status)

    def add_warning(self, warning: str) -> None:
        state = self.load()
        if warning not in state.warnings:
            state.warnings.append(warning)
        self.save()
