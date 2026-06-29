"""Phase 13 — MCP tool/resource semantic field tests."""

from __future__ import annotations

import pytest

from memory_engine.mcp.project_context import ProjectContext
from memory_engine.mcp.resources import resource_semantic_status
from memory_engine.mcp.schemas import MemoryStatusOutput, RetrievalMeta
from memory_engine.mcp.tools import tool_memory_status, tool_retrieve_agent_context
from memory_engine.mcp.schemas import RetrieveContextInput


@pytest.fixture()
def project_dir(tmp_path):
    (tmp_path / "README.md").write_text(
        "# Demo\n\nDemo project for semantic MCP tests.\n", encoding="utf-8"
    )
    return tmp_path


def test_retrieval_meta_has_semantic_defaults():
    meta = RetrievalMeta()
    assert meta.semantic_status == "disabled"
    assert meta.semantic_backend == "none"
    assert meta.embedding_provider == "none"


def test_memory_status_output_has_semantic_fields():
    out = MemoryStatusOutput(
        project_name="x", project_root="/x", bootstrap_status="READY",
        retrieval_mode="lexical_structured_fallback", vector_backend="ephemeral",
    )
    d = out.model_dump()
    for key in (
        "semantic_enabled", "semantic_backend", "embedding_provider",
        "embedding_model", "embedded_record_count", "pending_embedding_count",
        "semantic_health",
    ):
        assert key in d


def test_memory_status_tool_includes_semantic(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    ctx = ProjectContext(project_dir)
    status = tool_memory_status(ctx)
    assert "semantic_enabled" in status
    assert "semantic_health" in status
    assert status["semantic_enabled"] is False
    assert status["semantic_health"] == "disabled"


def test_retrieve_trace_includes_semantic_status(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    ctx = ProjectContext(project_dir)
    out = tool_retrieve_agent_context(ctx, RetrieveContextInput(task="explain project"))
    assert "semantic_status" in out["meta"]
    assert out["meta"]["semantic_status"] == "disabled"


def test_semantic_status_resource_valid(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    ctx = ProjectContext(project_dir)
    text = resource_semantic_status(ctx)
    assert "Semantic Retrieval Status" in text
    assert "retrieval_mode" in text
    assert "semantic_health" in text
