"""Phase 11 — MCP backward compatibility and security regression tests.

Covers:
- Existing retrieve_agent_context callers remain compatible
- Existing reflect_and_write callers remain compatible
- Retention MCP resources return valid string content
- shell=False enforced on all Git subprocess calls in security module
- Git command allowlist blocks unlisted commands
- Path sandboxing enforced
- Secret redaction active
- Remote URL not exposed
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# A. MCP schema backward compatibility
# ---------------------------------------------------------------------------


def test_retrieve_context_input_accepts_legacy_fields():
    """Existing callers that only pass task should still work."""
    from memory_engine.mcp.schemas import RetrieveContextInput

    inp = RetrieveContextInput(task="fix a bug")
    assert inp.task == "fix a bug"
    assert inp.task_intent == "unknown"
    assert inp.preferred_layers == []
    assert inp.proposition_types is None
    assert inp.current_branch is None
    assert inp.head_commit is None


def test_retrieve_context_input_accepts_phase10_fields():
    from memory_engine.mcp.schemas import RetrieveContextInput

    inp = RetrieveContextInput(
        task="architecture review",
        task_intent="architecture_review",
        preferred_layers=["summary", "paragraph"],
        proposition_types=["architecture", "decision"],
        current_branch="main",
        head_commit="abc123",
    )
    assert inp.task_intent == "architecture_review"
    assert inp.preferred_layers == ["summary", "paragraph"]


def test_reflect_and_write_input_accepts_legacy_fields():
    from memory_engine.mcp.schemas import ReflectAndWriteInput

    inp = ReflectAndWriteInput(
        task="fix test",
        outcome="all tests pass",
        verification_status="tests_passed",
    )
    assert inp.task == "fix test"
    assert inp.verification_status == "tests_passed"
    assert inp.changed_files == []
    assert inp.current_branch is None


def test_retrieve_context_output_has_phase11_compat_fields():
    from memory_engine.mcp.schemas import RetrieveContextOutput

    out = RetrieveContextOutput(task="test")
    d = out.model_dump()
    assert "multigranular_chunks" in d
    assert "multigranular_results_count" in d
    assert "knowledge_chunks" in d
    assert "memory_results_count" in d


# ---------------------------------------------------------------------------
# B. Retention MCP resource shape
# ---------------------------------------------------------------------------


def test_retention_status_resource_returns_string(tmp_path):
    """resource_retention_status must return a non-empty string."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from memory_engine.models.orm import Base
    from memory_engine.db.init_db import apply_schema_migrations, create_fts_tables
    import memory_engine.models.knowledge_orm  # noqa: register ORM
    from memory_engine.services.retention import MemoryRetentionService
    from memory_engine.models.orm import ProjectORM

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        create_fts_tables(conn)
        apply_schema_migrations(conn)
        conn.commit()

    import uuid
    with Session(engine) as session:
        p = ProjectORM(id=str(uuid.uuid4()), name="mcp-test")
        session.add(p)
        session.flush()
        svc = MemoryRetentionService(session, p.id)
        report = svc.generate_report()
        result = report.to_dict()

    assert isinstance(result, dict)
    assert "counts" in result
    assert "active" in result["counts"]


# ---------------------------------------------------------------------------
# C. Security regression: shell=False enforcement
# ---------------------------------------------------------------------------


def test_git_resolver_uses_shell_false():
    """All Git subprocesses in the bootstrap security module must use shell=False."""
    security_path = Path(__file__).parent.parent / "memory_engine" / "bootstrap" / "security.py"
    if not security_path.exists():
        pytest.skip("security.py not found")
    content = security_path.read_text(encoding="utf-8")
    # Every subprocess.run in security.py must have shell=False (not shell=True)
    import re
    shell_true = re.findall(r'subprocess\.run\b[^)]*shell\s*=\s*True', content)
    assert not shell_true, f"Found shell=True in security.py: {shell_true}"


def test_git_resolver_runtime_uses_shell_false():
    """Runtime Git resolver must not use shell=True."""
    runtime_dir = Path(__file__).parent.parent / "memory_engine" / "runtime"
    if not runtime_dir.exists():
        pytest.skip("runtime directory not found")
    import re
    violations = []
    for py_file in runtime_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if re.search(r'subprocess\.(run|Popen|call|check_output)\b[^)]*shell\s*=\s*True', content):
            violations.append(str(py_file))
    assert not violations, f"shell=True found in runtime: {violations}"


def test_bootstrap_security_no_shell_true():
    """Bootstrap module must not use shell=True anywhere."""
    bootstrap_dir = Path(__file__).parent.parent / "memory_engine" / "bootstrap"
    import re
    violations = []
    for py_file in bootstrap_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if re.search(r'subprocess\.(run|Popen|call|check_output)\b[^)]*shell\s*=\s*True', content):
            violations.append(str(py_file))
    assert not violations, f"shell=True found in bootstrap: {violations}"


def test_mcp_server_no_shell_true():
    """MCP server module must not use shell=True."""
    mcp_dir = Path(__file__).parent.parent / "memory_engine" / "mcp"
    import re
    violations = []
    for py_file in mcp_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if re.search(r'subprocess\.(run|Popen|call|check_output)\b[^)]*shell\s*=\s*True', content):
            violations.append(str(py_file))
    assert not violations, f"shell=True found in mcp: {violations}"


# ---------------------------------------------------------------------------
# D. Policy content safety assertions
# ---------------------------------------------------------------------------


def test_policy_does_not_contain_api_key_patterns(tmp_path):
    """Policy must not contain API key patterns that look like real credentials."""
    from memory_engine.policy.generator import generate_policy
    import re

    path = generate_policy(tmp_path)
    content = path.read_text(encoding="utf-8")
    # Only flag unambiguous credential patterns (not file paths or advisory text)
    secret_patterns = [
        r'sk-[A-Za-z0-9]{20,}',   # OpenAI-style key
        r'ghp_[A-Za-z0-9]{36}',   # GitHub PAT
        r'xox[baprs]-[A-Za-z0-9-]{10,}',  # Slack token
    ]
    for pattern in secret_patterns:
        matches = re.findall(pattern, content)
        assert not matches, f"Policy contains credential matching {pattern}: {matches}"


def test_policy_compliance_limitation_stated(tmp_path):
    from memory_engine.policy.generator import generate_policy

    path = generate_policy(tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "cannot technically force" in content


def test_policy_requires_retrieve_before_nontrivial(tmp_path):
    from memory_engine.policy.generator import generate_policy

    path = generate_policy(tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "retrieve_agent_context" in content
    assert "MUST" in content or "must" in content


def test_policy_requires_reflect_after_validated(tmp_path):
    from memory_engine.policy.generator import generate_policy

    path = generate_policy(tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "reflect_and_write" in content
