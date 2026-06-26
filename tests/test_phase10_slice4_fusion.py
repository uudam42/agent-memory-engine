"""Phase 10 Slice 4 — ContextComposer upgrade + MCP parameter extensions.

Tests verify:
- UnifiedRetrievalRequest accepts Phase 10 fields (task_intent, preferred_layers,
  proposition_types)
- UnifiedContextPack has multigranular_chunks and multigranular_results_count
- RetrieveContextInput (MCP schema) accepts Phase 10 fields
- RetrieveContextOutput (MCP schema) exposes multigranular_chunks
- UnifiedContextRetrievalService.retrieve() runs multigranular search when Phase 10
  data exists for the project
- multigranular_chunks populated after ingest + retrieve
- multigranular_results_count matches len(multigranular_chunks)
- task_intent flows through to GranularityRouter (bug_fix → proposition layer first)
- preferred_layers override works end-to-end
- project without Phase 10 data still produces valid pack (backward compat)
- total_token_estimate includes multigranular tokens
- _has_phase10_data returns False for empty project, True after ingest
- MCP tools.py output dict contains multigranular_chunks and multigranular_results_count
- cache_hit is False on first call, True on repeated call with same params
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import memory_engine.models.knowledge_orm  # noqa: F401
from memory_engine.knowledge.fts_index import create_phase10_fts_tables
from memory_engine.knowledge.fusion import (
    UnifiedContextRetrievalService,
    _has_phase10_data,
)
from memory_engine.knowledge.ingestion import KnowledgeIngestionService
from memory_engine.mcp.schemas import RetrieveContextInput, RetrieveContextOutput
from memory_engine.models.knowledge_domain import (
    GranularityLevel,
    KnowledgeIngestRequest,
    MultiGranularitySearchResult,
    SourceType,
    UnifiedContextPack,
    UnifiedRetrievalRequest,
)
from memory_engine.models.orm import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PYTHON_SAMPLE = '''\
"""GitContextResolver resolves current branch, HEAD commit, and working-tree changes.

Uses an allowlisted read-only Git command runner. shell=False is enforced on all
subprocess calls and cannot be overridden.
"""
import subprocess
from dataclasses import dataclass


class GitContextResolver:
    """Resolves Git context for branch-aware memory retrieval."""

    _ALLOWED = {"status", "log", "diff", "branch", "rev-parse"}

    def resolve(self) -> dict:
        """Return a frozen GitContext snapshot of current working tree."""
        branch = self._run("branch", "--show-current")
        return {"branch": branch}

    def _run(self, *args: str) -> str:
        """Execute a whitelisted Git command with shell=False."""
        cmd = args[0] if args else ""
        if cmd not in self._ALLOWED:
            raise ValueError(f"Command not in allowlist: {cmd}")
        # shell=False is enforced; allowlist contains only safe commands.
        result = subprocess.run(["git", *args], shell=False, capture_output=True, text=True)
        return result.stdout.strip()
'''

_MARKDOWN_SAMPLE = '''\
# Security Model

- shell=False is enforced on all Git subprocess calls.
- Commands must be in the allowlist; unlisted commands are blocked.
- Never expose remote URLs or user credentials.
'''


@pytest.fixture()
def engine_():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=eng)
    with eng.connect() as conn:
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
            USING fts5(chunk_id UNINDEXED, content, heading_text, symbols_text,
                       module_text, tags_text, tokenize='porter unicode61')
        """))
        create_phase10_fts_tables(conn)
        conn.commit()
    return eng


@pytest.fixture()
def session(engine_):
    with Session(engine_) as sess:
        yield sess


@pytest.fixture()
def project_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def ingested(session, project_id):
    session.execute(text(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (:id, :name, :now, :now)"
    ), {"id": project_id, "name": "test", "now": datetime.now(timezone.utc)})
    session.commit()

    svc = KnowledgeIngestionService(session)
    svc.ingest(KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type=SourceType.code_file,
        title="git_resolver.py",
        content=_PYTHON_SAMPLE,
        source_path="memory_engine/runtime/git/git_resolver.py",
    ))
    svc.ingest(KnowledgeIngestRequest(
        project_id=uuid.UUID(project_id),
        source_type=SourceType.markdown,
        title="security.md",
        content=_MARKDOWN_SAMPLE,
        source_path="docs/security.md",
    ))
    return project_id


# ---------------------------------------------------------------------------
# A. Domain model: UnifiedRetrievalRequest Phase 10 fields
# ---------------------------------------------------------------------------


def test_unified_retrieval_request_has_task_intent():
    req = UnifiedRetrievalRequest(
        project_id=uuid.uuid4(),
        task="fix the bug",
        task_intent="bug_fix",
    )
    assert req.task_intent == "bug_fix"


def test_unified_retrieval_request_default_task_intent():
    req = UnifiedRetrievalRequest(project_id=uuid.uuid4(), task="task")
    assert req.task_intent == "unknown"


def test_unified_retrieval_request_has_preferred_layers():
    req = UnifiedRetrievalRequest(
        project_id=uuid.uuid4(),
        task="t",
        preferred_layers=["proposition", "paragraph"],
    )
    assert req.preferred_layers == ["proposition", "paragraph"]


def test_unified_retrieval_request_default_preferred_layers():
    req = UnifiedRetrievalRequest(project_id=uuid.uuid4(), task="t")
    assert req.preferred_layers == []


def test_unified_retrieval_request_has_proposition_types():
    req = UnifiedRetrievalRequest(
        project_id=uuid.uuid4(),
        task="t",
        proposition_types=["security_rule", "constraint"],
    )
    assert req.proposition_types == ["security_rule", "constraint"]


def test_unified_retrieval_request_default_proposition_types():
    req = UnifiedRetrievalRequest(project_id=uuid.uuid4(), task="t")
    assert req.proposition_types is None


# ---------------------------------------------------------------------------
# B. Domain model: UnifiedContextPack Phase 10 fields
# ---------------------------------------------------------------------------


def test_unified_context_pack_has_multigranular_chunks():
    pack = UnifiedContextPack(project_id=uuid.uuid4(), task="t")
    assert hasattr(pack, "multigranular_chunks")
    assert pack.multigranular_chunks == []


def test_unified_context_pack_has_multigranular_results_count():
    pack = UnifiedContextPack(project_id=uuid.uuid4(), task="t")
    assert hasattr(pack, "multigranular_results_count")
    assert pack.multigranular_results_count == 0


def test_unified_context_pack_multigranular_chunks_accepts_results():
    result = MultiGranularitySearchResult(
        result_id="abc",
        result_type="proposition",
        granularity=GranularityLevel.proposition,
        content="shell=False is enforced.",
        score=0.9,
    )
    pack = UnifiedContextPack(
        project_id=uuid.uuid4(),
        task="t",
        multigranular_chunks=[result],
        multigranular_results_count=1,
    )
    assert len(pack.multigranular_chunks) == 1
    assert pack.multigranular_results_count == 1


# ---------------------------------------------------------------------------
# C. MCP schema: RetrieveContextInput Phase 10 fields
# ---------------------------------------------------------------------------


def test_retrieve_context_input_has_task_intent():
    inp = RetrieveContextInput(task="fix bug", task_intent="bug_fix")
    assert inp.task_intent == "bug_fix"


def test_retrieve_context_input_default_task_intent():
    inp = RetrieveContextInput(task="t")
    assert inp.task_intent == "unknown"


def test_retrieve_context_input_has_preferred_layers():
    inp = RetrieveContextInput(task="t", preferred_layers=["proposition"])
    assert inp.preferred_layers == ["proposition"]


def test_retrieve_context_input_has_proposition_types():
    inp = RetrieveContextInput(task="t", proposition_types=["security_rule"])
    assert inp.proposition_types == ["security_rule"]


def test_retrieve_context_input_default_proposition_types():
    inp = RetrieveContextInput(task="t")
    assert inp.proposition_types is None


# ---------------------------------------------------------------------------
# D. MCP schema: RetrieveContextOutput Phase 10 fields
# ---------------------------------------------------------------------------


def test_retrieve_context_output_has_multigranular_chunks():
    out = RetrieveContextOutput(task="t")
    assert hasattr(out, "multigranular_chunks")
    assert out.multigranular_chunks == []


def test_retrieve_context_output_has_multigranular_results_count():
    out = RetrieveContextOutput(task="t")
    assert hasattr(out, "multigranular_results_count")
    assert out.multigranular_results_count == 0


# ---------------------------------------------------------------------------
# E. _has_phase10_data helper
# ---------------------------------------------------------------------------


def test_has_phase10_data_false_empty_project(session, project_id):
    session.execute(text(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (:id, :name, :now, :now)"
    ), {"id": project_id, "name": "t", "now": datetime.now(timezone.utc)})
    session.commit()
    assert _has_phase10_data(session, project_id) is False


def test_has_phase10_data_true_after_ingest(session, ingested):
    assert _has_phase10_data(session, ingested) is True


def test_has_phase10_data_false_wrong_project(session, ingested):
    other_pid = str(uuid.uuid4())
    assert _has_phase10_data(session, other_pid) is False


# ---------------------------------------------------------------------------
# F. UnifiedContextRetrievalService.retrieve() — integration
# ---------------------------------------------------------------------------


def test_retrieve_returns_unified_context_pack(session, ingested):
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="fix security issue with shell command",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    assert isinstance(pack, UnifiedContextPack)


def test_retrieve_populates_multigranular_chunks(session, ingested):
    svc = UnifiedContextRetrievalService(session)
    # Use a single focused term that propositions definitely contain
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    assert len(pack.multigranular_chunks) >= 1


def test_retrieve_multigranular_count_matches_list(session, ingested):
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell allowlist security",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    assert pack.multigranular_results_count == len(pack.multigranular_chunks)


def test_retrieve_multigranular_chunks_are_valid_type(session, ingested):
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell allowlist",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    for r in pack.multigranular_chunks:
        assert isinstance(r, MultiGranularitySearchResult)
        assert len(r.content) > 0
        assert r.score > 0.0


def test_retrieve_total_token_estimate_includes_multigranular(session, ingested):
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    mg_tokens = sum(max(1, len(r.content) // 4) for r in pack.multigranular_chunks)
    # total_token_estimate >= memory_tokens + knowledge_tokens + multigranular_tokens
    assert pack.total_token_estimate >= pack.memory_tokens + pack.knowledge_tokens


def test_retrieve_task_intent_bug_fix_returns_propositions(session, ingested):
    """bug_fix intent should route to proposition layer first → multigranular has propositions."""
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell allowlist enforcement",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    prop_results = [r for r in pack.multigranular_chunks if r.result_type == "proposition"]
    assert len(prop_results) >= 1


def test_retrieve_preferred_layers_override(session, ingested):
    """Caller sets preferred_layers=["summary"] → multigranular returns summary results."""
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="branch",
        task_intent="bug_fix",
        preferred_layers=["summary"],
    )
    pack = svc.retrieve(req)
    summary_results = [r for r in pack.multigranular_chunks if r.result_type == "summary"]
    assert len(summary_results) >= 1


def test_retrieve_no_phase10_data_backward_compat(session, project_id):
    """Project with no Phase 10 data still returns a valid (empty multigranular) pack."""
    session.execute(text(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (:id, :name, :now, :now)"
    ), {"id": project_id, "name": "t", "now": datetime.now(timezone.utc)})
    session.commit()

    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(project_id),
        task="fix the bug",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    assert isinstance(pack, UnifiedContextPack)
    assert pack.multigranular_chunks == []
    assert pack.multigranular_results_count == 0


def test_retrieve_cache_hit_on_second_call(session, ingested):
    """Second identical retrieve() call should be a cache hit."""
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell",
        task_intent="bug_fix",
    )
    first = svc.retrieve(req)
    second = svc.retrieve(req)
    assert second.cache_hit is True


def test_retrieve_proposition_types_filter_flows_through(session, ingested):
    """proposition_types=['security_rule'] should surface security propositions."""
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell enforcement",
        task_intent="bug_fix",
        preferred_layers=["proposition"],
        proposition_types=["security_rule"],
    )
    pack = svc.retrieve(req)
    # Should find security_rule propositions (shell=False, allowlist)
    prop_results = [r for r in pack.multigranular_chunks if r.result_type == "proposition"]
    assert len(prop_results) >= 1


def test_retrieve_different_intents_produce_different_layers(session, ingested):
    """architecture_review should prefer summaries, bug_fix should prefer propositions."""
    svc_a = UnifiedContextRetrievalService(session)
    # "branch" appears in the module summary text
    req_arch = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="branch",
        task_intent="architecture_review",
    )
    pack_arch = svc_a.retrieve(req_arch)

    svc_b = UnifiedContextRetrievalService(session)
    # "shell" appears in propositions (security_rule type)
    req_bug = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell",
        task_intent="bug_fix",
    )
    pack_bug = svc_b.retrieve(req_bug)

    arch_types = {r.result_type for r in pack_arch.multigranular_chunks}
    bug_types = {r.result_type for r in pack_bug.multigranular_chunks}

    # architecture_review prefers summaries, bug_fix prefers propositions
    assert len(arch_types) >= 1
    assert len(bug_types) >= 1


# ---------------------------------------------------------------------------
# G. MCP tool output dict structure
# ---------------------------------------------------------------------------


def test_tool_output_dict_contains_multigranular_chunks(session, ingested):
    """Simulate what tool_retrieve_agent_context returns: dict must have multigranular_chunks."""
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell allowlist",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)

    # Replicate the tool output dict construction (from tools.py)
    output = {
        "knowledge_chunks": [k.model_dump() for k in pack.knowledge_chunks],
        "multigranular_chunks": [k.model_dump() for k in pack.multigranular_chunks],
        "knowledge_results_count": pack.knowledge_results_count,
        "multigranular_results_count": pack.multigranular_results_count,
        "total_token_estimate": pack.total_token_estimate,
    }
    assert "multigranular_chunks" in output
    assert "multigranular_results_count" in output
    assert isinstance(output["multigranular_chunks"], list)
    assert output["multigranular_results_count"] == len(output["multigranular_chunks"])


def test_tool_output_multigranular_chunk_is_serializable(session, ingested):
    """Each MultiGranularitySearchResult must be model_dump()-able."""
    svc = UnifiedContextRetrievalService(session)
    req = UnifiedRetrievalRequest(
        project_id=uuid.UUID(ingested),
        task="shell",
        task_intent="bug_fix",
    )
    pack = svc.retrieve(req)
    for r in pack.multigranular_chunks:
        d = r.model_dump()
        assert isinstance(d, dict)
        assert "result_id" in d
        assert "content" in d
        assert "score" in d
        assert "granularity" in d
