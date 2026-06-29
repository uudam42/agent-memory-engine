"""Phase 13 — `memory semantic` CLI tests."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from memory_engine.cli import app

runner = CliRunner()


@pytest.fixture()
def project_dir(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n\nA demo project.\n", encoding="utf-8")
    return tmp_path


def test_semantic_status_runs(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    result = runner.invoke(app, ["semantic", "status", "--project-root", str(project_dir)])
    assert result.exit_code == 0
    assert "Semantic Retrieval Status" in result.stdout
    assert "retrieval_mode" in result.stdout


def test_semantic_doctor_runs(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    result = runner.invoke(app, ["semantic", "doctor", "--project-root", str(project_dir)])
    assert result.exit_code == 0
    assert "Semantic Doctor" in result.stdout
    assert "sqlite_vec importable" in result.stdout


def test_semantic_reindex_without_provider_is_noop(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    result = runner.invoke(app, ["semantic", "reindex", "--project-root", str(project_dir)])
    assert result.exit_code == 0
    assert "not active" in result.stdout.lower()


def test_semantic_clear_requires_confirm(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    result = runner.invoke(app, ["semantic", "clear", "--project-root", str(project_dir)])
    assert result.exit_code == 1
    assert "confirm" in result.stdout.lower()


def test_semantic_clear_with_confirm_no_backend(project_dir, monkeypatch):
    monkeypatch.delenv("MEMORY_ENGINE_SEMANTIC_ENABLED", raising=False)
    result = runner.invoke(
        app, ["semantic", "clear", "--project-root", str(project_dir), "--confirm"]
    )
    assert result.exit_code == 0
    assert "nothing to clear" in result.stdout.lower()
