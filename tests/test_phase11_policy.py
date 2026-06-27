"""Phase 11 — Policy generation and installer tests.

Covers:
- AGENT_MEMORY_POLICY.md generation
- Idempotent regeneration (stable begin/end markers, no duplicate blocks)
- Policy version stamping
- User-authored content preserved outside markers
- Claude Code adapter installation and removal
- Cursor adapter installation and removal
- Adapter status diagnostics
- policy_status helper
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memory_engine.policy.generator import (
    POLICY_VERSION,
    _BEGIN_MARKER,
    _END_MARKER,
    generate_policy,
    policy_status,
)
from memory_engine.policy.installer import (
    _CLAUDE_MD_BEGIN,
    _CLAUDE_MD_END,
    adapter_status,
    install_claude_code,
    install_cursor,
    remove_adapter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """A minimal temporary project directory."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    return tmp_path


# ---------------------------------------------------------------------------
# A. Policy generation
# ---------------------------------------------------------------------------


def test_generate_policy_creates_file(tmp_project):
    path = generate_policy(tmp_project)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert _BEGIN_MARKER in content
    assert _END_MARKER in content


def test_generated_policy_contains_version(tmp_project):
    path = generate_policy(tmp_project)
    content = path.read_text(encoding="utf-8")
    assert f"policy_version: {POLICY_VERSION}" in content


def test_generated_policy_contains_required_sections(tmp_project):
    path = generate_policy(tmp_project)
    content = path.read_text(encoding="utf-8")
    required = [
        "Purpose",
        "Task Classification",
        "Mandatory Pre-Task Recall Rule",
        "Mandatory Post-Task Reflection Rule",
        "Source-of-Truth and Safety Rules",
        "Branch-Aware Rules",
        "Retrieval Granularity Rules",
        "Compliance Checklist",
        "Transparency Notice",
    ]
    for section in required:
        assert section in content, f"Missing section: {section}"


def test_generate_policy_idempotent_no_duplicate_markers(tmp_project):
    generate_policy(tmp_project)
    generate_policy(tmp_project)  # second call

    path = tmp_project / ".memory-engine" / "generated" / "AGENT_MEMORY_POLICY.md"
    content = path.read_text(encoding="utf-8")
    assert content.count(_BEGIN_MARKER) == 1
    assert content.count(_END_MARKER) == 1


def test_generate_policy_preserves_user_content_before_marker(tmp_project):
    out_dir = tmp_project / ".memory-engine" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    policy_path = out_dir / "AGENT_MEMORY_POLICY.md"
    user_prefix = "# My Custom Header\n\nSome user notes.\n\n"
    policy_path.write_text(user_prefix + _BEGIN_MARKER + "\nold content\n" + _END_MARKER)

    generate_policy(tmp_project)
    content = policy_path.read_text(encoding="utf-8")
    assert content.startswith(user_prefix)
    assert _BEGIN_MARKER in content


def test_policy_status_before_generation(tmp_project):
    status = policy_status(tmp_project)
    assert status["exists"] is False


def test_policy_status_after_generation(tmp_project):
    generate_policy(tmp_project)
    status = policy_status(tmp_project)
    assert status["exists"] is True
    assert status["has_markers"] is True
    assert status["policy_version"] == POLICY_VERSION


def test_generate_policy_custom_output_path(tmp_project, tmp_path):
    custom = tmp_path / "out" / "policy.md"
    custom.parent.mkdir(parents=True)
    path = generate_policy(tmp_project, output_path=custom)
    assert path == custom
    assert custom.exists()


# ---------------------------------------------------------------------------
# B. Claude Code adapter
# ---------------------------------------------------------------------------


def test_install_claude_code_creates_claude_md(tmp_project):
    install_claude_code(tmp_project)
    claude_md = tmp_project / "CLAUDE.md"
    assert claude_md.exists()
    content = claude_md.read_text(encoding="utf-8")
    assert _CLAUDE_MD_BEGIN in content
    assert _CLAUDE_MD_END in content
    assert "retrieve_agent_context" in content


def test_install_claude_code_idempotent(tmp_project):
    install_claude_code(tmp_project)
    install_claude_code(tmp_project)
    claude_md = tmp_project / "CLAUDE.md"
    content = claude_md.read_text(encoding="utf-8")
    assert content.count(_CLAUDE_MD_BEGIN) == 1


def test_install_claude_code_preserves_user_content(tmp_project):
    claude_md = tmp_project / "CLAUDE.md"
    existing_content = "# My existing project rules\n\nDo not touch this.\n"
    claude_md.write_text(existing_content)

    install_claude_code(tmp_project)
    content = claude_md.read_text(encoding="utf-8")
    assert "Do not touch this." in content
    assert _CLAUDE_MD_BEGIN in content


def test_remove_claude_code_adapter(tmp_project):
    install_claude_code(tmp_project)
    result = remove_adapter(tmp_project, "claude-code")
    assert result is not None

    claude_md = tmp_project / "CLAUDE.md"
    content = claude_md.read_text(encoding="utf-8")
    assert _CLAUDE_MD_BEGIN not in content


# ---------------------------------------------------------------------------
# C. Cursor adapter
# ---------------------------------------------------------------------------


def test_install_cursor_creates_rule_file(tmp_project):
    install_cursor(tmp_project)
    rule_file = tmp_project / ".cursor" / "rules" / "agent-memory-policy.mdc"
    assert rule_file.exists()
    content = rule_file.read_text(encoding="utf-8")
    assert _BEGIN_MARKER in content
    assert "retrieve_agent_context" in content


def test_install_cursor_idempotent(tmp_project):
    install_cursor(tmp_project)
    install_cursor(tmp_project)
    rule_file = tmp_project / ".cursor" / "rules" / "agent-memory-policy.mdc"
    content = rule_file.read_text(encoding="utf-8")
    assert content.count(_BEGIN_MARKER) == 1


def test_install_cursor_preserves_existing_rules(tmp_project):
    rules_dir = tmp_project / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    rule_file = rules_dir / "agent-memory-policy.mdc"
    rule_file.write_text("# Existing cursor rules\n\nKeep this.\n")

    install_cursor(tmp_project)
    content = rule_file.read_text(encoding="utf-8")
    assert "Keep this." in content
    assert _BEGIN_MARKER in content


def test_remove_cursor_adapter(tmp_project):
    install_cursor(tmp_project)
    result = remove_adapter(tmp_project, "cursor")
    assert result is not None

    rule_file = tmp_project / ".cursor" / "rules" / "agent-memory-policy.mdc"
    content = rule_file.read_text(encoding="utf-8")
    assert _BEGIN_MARKER not in content


# ---------------------------------------------------------------------------
# D. adapter_status diagnostics
# ---------------------------------------------------------------------------


def test_adapter_status_before_installation(tmp_project):
    status = adapter_status(tmp_project)
    assert status["claude_code"]["installed"] is False
    assert status["cursor"]["installed"] is False
    assert status["policy"]["exists"] is False


def test_adapter_status_after_installation(tmp_project):
    install_claude_code(tmp_project)
    install_cursor(tmp_project)
    status = adapter_status(tmp_project)
    assert status["claude_code"]["installed"] is True
    assert status["cursor"]["installed"] is True
    assert status["policy"]["exists"] is True


def test_remove_nonexistent_adapter_returns_none(tmp_project):
    result = remove_adapter(tmp_project, "claude-code")
    assert result is None


def test_remove_unknown_client_returns_none(tmp_project):
    result = remove_adapter(tmp_project, "unknown-client")
    assert result is None
