"""Security and correctness regression tests.

These tests reproduce CONFIRMED bugs in the current codebase. Each test is
expected to FAIL before the corresponding fix is applied.

Confirmed bugs:
  Bug A: RecallRequest model has no current_branch field
  Bug B: RecallService.recall() does not pass current_branch to DeterministicRanker
  Bug C: fusion.py builds RecallRequest without current_branch
  Bug D: No minimum relevance gate — unrelated memories returned
  Bug E: Project identity uses directory basename only, no fingerprint
  Bug F: SimpleCache.make_key() has no memory_generation parameter
  Bug G: Stale/superseded memories may appear as selected in retrieval

Run with:  pytest tests/test_security_correctness.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, call
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from memory_engine.db.init_db import apply_schema_migrations, create_fts_tables
from memory_engine.models.domain import (
    MemoryKind,
    MemoryNode,
    MemoryNodeCreate,
    MemoryStatus,
    ProjectCreate,
    RecallRequest,
    TaskIntent,
)
from memory_engine.models.orm import Base, MemoryNodeORM
from memory_engine.repositories.memory_node import MemoryNodeRepository
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.skills.ranker import DeterministicRanker
from memory_engine.skills.recall import RecallService


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=_engine)
    with _engine.connect() as conn:
        create_fts_tables(conn)
        apply_schema_migrations(conn)
        conn.commit()
    yield _engine
    Base.metadata.drop_all(bind=_engine)
    _engine.dispose()


@pytest.fixture()
def session(engine) -> Session:
    _Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = _Session()
    yield s
    s.close()


@pytest.fixture()
def project_service(session):
    return ProjectService(session)


@pytest.fixture()
def memory_service(session):
    return MemoryService(session)


@pytest.fixture()
def recall_service(session):
    return RecallService(session)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_memory_node(
    project_id: uuid.UUID,
    title: str,
    summary: str,
    kind: MemoryKind = MemoryKind.decision,
    importance: float = 0.9,
    confidence: float = 0.9,
    tags: list[str] | None = None,
    branch_name: str | None = None,
    branch_scope: str | None = "global",
    status: str = "active",
) -> MemoryNode:
    """Build a MemoryNode domain object (not persisted) for ranker unit tests."""
    n = MemoryNode(
        id=uuid.uuid4(),
        project_id=project_id,
        parent_id=None,
        title=title,
        summary=summary,
        kind=kind,
        depth=0,
        tags=tags or [],
        status=MemoryStatus(status),
        confidence=confidence,
        importance=importance,
        created_at=_now(),
        updated_at=_now(),
        evidence=[],
    )
    n.branch_name = branch_name
    n.branch_scope = branch_scope
    return n


# ---------------------------------------------------------------------------
# Bug A: RecallRequest model has no current_branch field
# ---------------------------------------------------------------------------

class TestBugA_RecallRequestMissingBranch:
    """
    RecallRequest (models/domain.py) does not declare current_branch.
    Pydantic silently discards extra kwargs, so branch context is lost
    at the model boundary without any error.
    """

    def test_recall_request_missing_current_branch_field(self):
        """Bug A: RecallRequest.model_fields must contain 'current_branch'.
        FAILS before fix; PASSES after fix."""
        fields = RecallRequest.model_fields
        assert "current_branch" in fields, (
            "Bug A: RecallRequest is missing the 'current_branch' field. "
            "Branch-aware ranking is completely bypassed because the branch "
            "context cannot be carried through the recall request model."
        )

    def test_recall_request_silently_drops_branch_kwargs(self):
        """Confirm the current silent-drop behaviour so we know what to fix."""
        proj_id = uuid.uuid4()
        req = RecallRequest(
            project_id=proj_id,
            current_task="test task",
            current_branch="feature/grpc",  # type: ignore[call-arg]
        )
        # Extra fields are silently dropped — this proves the field is absent
        has_attr = hasattr(req, "current_branch") and getattr(req, "current_branch", None) == "feature/grpc"
        if not has_attr:
            pytest.xfail(
                "Confirmed Bug A: current_branch kwarg is silently discarded by Pydantic. "
                "After fix, this test should no longer xfail."
            )


# ---------------------------------------------------------------------------
# Bug B: RecallService.recall() does not pass current_branch to ranker
# ---------------------------------------------------------------------------

class TestBugB_RecallServiceIgnoresBranch:
    """
    DeterministicRanker.rank() accepts current_branch but RecallService.recall()
    never passes it. The Phase 9 branch signals (branch_affinity,
    revision_validity, branch_scope_priority, etc.) are therefore always at
    their default values regardless of which branch is active.
    """

    def test_ranker_called_without_current_branch(
        self, project_service, memory_service, recall_service, session
    ):
        """Patch DeterministicRanker.rank to capture call kwargs.
        Confirms Bug B: recall() never passes current_branch to rank()."""
        proj = project_service.create(
            ProjectCreate(name="bug-b-project", description="")
        )
        project_uuid = proj.id

        # Create a node so recall has something to rank
        memory_service.create_node(MemoryNodeCreate(
            project_id=project_uuid,
            title="API design decision",
            summary="Use REST endpoints for all public APIs.",
            kind=MemoryKind.decision,
            importance=0.8,
            confidence=0.9,
            tags=["api", "rest"],
        ))

        req = RecallRequest(
            project_id=project_uuid,
            current_task="How should we design our API endpoints?",
            current_branch="feature/grpc",
        )

        ranker_calls: list[dict] = []
        original_rank = DeterministicRanker.rank

        def capturing_rank(self, nodes, **kwargs):  # type: ignore[no-untyped-def]
            ranker_calls.append(kwargs)
            return original_rank(self, nodes, **kwargs)

        with patch.object(DeterministicRanker, "rank", capturing_rank):
            recall_service.recall(req)

        assert len(ranker_calls) == 1, "Ranker should have been called once"
        branch_passed = ranker_calls[0].get("current_branch")

        # Bug B (fixed): current_branch from RecallRequest must reach rank().
        # Before fix: always None (RecallService never read or passed the field).
        # After fix: must equal "feature/grpc".
        assert branch_passed == "feature/grpc", (
            f"Bug B: RecallService.recall() did not propagate current_branch to "
            f"DeterministicRanker.rank(). Got: {branch_passed!r}. "
            "The Phase 9 branch_affinity signal is never activated without this."
        )

    def test_feature_branch_node_ranks_above_mainline_when_on_that_branch(
        self, project_service, session
    ):
        """Integration test: when current_branch is 'feature/grpc', the node
        tagged as feature/grpc MUST rank above the main-branch node.

        This test exercises the ranker directly (bypassing RecallService Bug B)
        to confirm the ranker logic is correct and the bug is purely in
        the propagation layer.
        """
        project_uuid = uuid.uuid4()
        ranker = DeterministicRanker()

        grpc_node = _make_memory_node(
            project_uuid,
            title="Transport protocol",
            summary="Use gRPC for all service-to-service communication.",
            branch_name="feature/grpc",
            branch_scope="current_branch",
            tags=["grpc", "transport", "protocol"],
        )
        rest_node = _make_memory_node(
            project_uuid,
            title="Transport protocol",
            summary="Use REST for all service-to-service communication.",
            branch_name="main",
            branch_scope="mainline",
            tags=["rest", "transport", "protocol"],
        )

        # Without current_branch: both should rank similarly
        scored_no_branch = ranker.rank(
            [grpc_node, rest_node],
            task="What transport protocol for services?",
            intent=TaskIntent.architecture_review,
            current_files=[],
            current_symbols=[],
            current_branch=None,
        )
        # Both have same content quality, should be close
        scores_no_branch = [s.score for s in scored_no_branch]
        assert abs(scores_no_branch[0] - scores_no_branch[1]) < 0.3, \
            "Without branch context, both nodes should have similar scores"

        # With current_branch=feature/grpc: grpc node should score higher
        scored_with_branch = ranker.rank(
            [grpc_node, rest_node],
            task="What transport protocol for services?",
            intent=TaskIntent.architecture_review,
            current_files=[],
            current_symbols=[],
            current_branch="feature/grpc",
        )
        grpc_scored = next(s for s in scored_with_branch if str(s.node.id) == str(grpc_node.id))
        rest_scored = next(s for s in scored_with_branch if str(s.node.id) == str(rest_node.id))

        assert grpc_scored.score > rest_scored.score, (
            f"Ranker: gRPC node (score={grpc_scored.score:.4f}) must rank above "
            f"REST node (score={rest_scored.score:.4f}) when current_branch=feature/grpc. "
            "If this fails, the ranker's Phase 9 branch_affinity signal is not correctly "
            "boosting branch-matched memories — investigate DeterministicRanker.rank()."
        )


# ---------------------------------------------------------------------------
# Bug C: fusion.py drops current_branch when building RecallRequest
# ---------------------------------------------------------------------------

class TestBugC_FusionDropsBranch:
    """
    UnifiedContextRetrievalService.retrieve() accepts current_branch in
    UnifiedRetrievalRequest but builds RecallRequest without it.
    """

    def test_fusion_recall_request_missing_branch(self):
        """Inspect fusion.py source to confirm Bug C.

        current_branch=req.current_branch already appears in the source for
        cache key and KnowledgeSearchService — but NOT in the RecallRequest(...)
        construction. This test uses regex to find RecallRequest call sites
        specifically.
        """
        import inspect
        import re
        from memory_engine.knowledge import fusion as fusion_module
        source = inspect.getsource(fusion_module.UnifiedContextRetrievalService.retrieve)

        # Find all RecallRequest(…) call sites — match from `RecallRequest(` to
        # a balanced closing paren (good enough for non-nested args).
        recall_req_blocks = re.findall(
            r'RecallRequest\s*\([^)]+\)',
            source,
            re.DOTALL,
        )
        assert recall_req_blocks, "Cannot find RecallRequest(...) in fusion.retrieve — test is broken"

        has_branch_in_recall_req = any(
            "current_branch" in block for block in recall_req_blocks
        )
        assert has_branch_in_recall_req, (
            "Bug C: fusion.py builds RecallRequest without current_branch. "
            f"Found RecallRequest blocks: {recall_req_blocks}. "
            "Even after Bug A fix (adding current_branch to RecallRequest model), "
            "fusion.py must pass req.current_branch when constructing RecallRequest."
        )


# ---------------------------------------------------------------------------
# Bug D: No relevance gate — high-importance unrelated memories returned
# ---------------------------------------------------------------------------

class TestBugD_NoRelevanceGate:
    """
    RecallService loads ALL project nodes and ranks them. There is no minimum
    relevance threshold. A memory with importance=1.0 about an unrelated topic
    can appear in 'selected' context.
    """

    def test_high_importance_unrelated_memory_not_selected(
        self, project_service, memory_service, recall_service
    ):
        """Bug D: unrelated database memory must not appear in CSS animation query.

        FAILS before fix; PASSES after relevance gate is added."""
        proj = project_service.create(
            ProjectCreate(name="relevance-gate-project", description="")
        )
        project_uuid = proj.id

        memory_service.create_node(MemoryNodeCreate(
            project_id=project_uuid,
            title="Zero-downtime database migration strategy",
            summary=(
                "All schema migrations must use blue-green deploy with backward-compatible "
                "column adds. Never drop columns in the same PR. Use Flyway for tracking."
            ),
            kind=MemoryKind.architecture,
            importance=1.0,
            confidence=1.0,
            tags=["database", "migration", "schema", "flyway"],
        ))

        req = RecallRequest(
            project_id=project_uuid,
            current_task="How do I implement a CSS keyframe animation with easing functions?",
        )
        result = recall_service.recall(req)

        if result.recall_skipped:
            return  # Router correctly skipped — acceptable

        selected = [te for te in result.retrieval_trace if te.action == "selected"]
        assert len(selected) == 0, (
            f"Bug D: {len(selected)} unrelated memory node(s) selected for CSS query. "
            f"Titles: {[te.title for te in selected]}. "
            "A minimum relevance gate must prevent infrastructure memories "
            "from appearing in UI/animation queries."
        )

    def test_no_forced_topk_when_irrelevant(
        self, project_service, memory_service, recall_service
    ):
        """Bug D: when multiple memories exist but none match the query,
        the system must return empty rather than forcing k results."""
        proj = project_service.create(
            ProjectCreate(name="empty-topk-project", description="")
        )
        project_uuid = proj.id

        for title, summary, tags in [
            ("Kafka partitioning strategy",
             "Use 12 partitions per Kafka topic for balanced consumer load.",
             ["kafka", "partitions", "messaging"]),
            ("Redis TTL policy",
             "All cache keys must have TTL <= 24h. No permanent cache entries allowed.",
             ["redis", "cache", "ttl"]),
            ("PostgreSQL BRIN index",
             "Use BRIN indexes for append-only time-series columns over 10M rows.",
             ["postgres", "index", "brin", "database"]),
        ]:
            memory_service.create_node(MemoryNodeCreate(
                project_id=project_uuid,
                title=title,
                summary=summary,
                kind=MemoryKind.architecture,
                importance=0.9,
                confidence=0.9,
                tags=tags,
            ))

        req = RecallRequest(
            project_id=project_uuid,
            current_task="What font-family should I use for the hero section of the marketing page?",
        )
        result = recall_service.recall(req)

        if result.recall_skipped:
            return

        selected = [te for te in result.retrieval_trace if te.action == "selected"]
        assert len(selected) == 0, (
            f"Bug D: {len(selected)} infrastructure memory node(s) returned for typography query. "
            f"Titles: {[te.title for te in selected]}. "
            "No minimum relevance gate exists — system forces top-k without relevance check."
        )


# ---------------------------------------------------------------------------
# Bug E: Project identity uses basename only — no repository fingerprint
# ---------------------------------------------------------------------------

class TestBugE_ProjectIdentity:
    """
    ProjectORM.name = directory_basename (unique within one DB).
    ProjectContext.get_project_id() looks up project by basename.
    There is no repository fingerprint to detect database copies.
    """

    def test_project_id_lookup_uses_only_basename(self):
        """Bug E: get_project_id() source must reference a fingerprint or
        canonical path, not just .name (directory basename)."""
        import inspect
        from memory_engine.mcp.project_context import ProjectContext, clear_registry
        source = inspect.getsource(ProjectContext.get_project_id)

        # Bug E: only "name" (basename) is used, no fingerprint
        uses_fingerprint = (
            "fingerprint" in source
            or "canonical" in source
            or "git_root" in source
            or "repo_root" in source
        )
        assert uses_fingerprint, (
            "Bug E: ProjectContext.get_project_id() uses only directory basename "
            "for project lookup (filter_by(name=...)). Two different repositories "
            "with the same directory name would share a project row in the same DB. "
            "A stable fingerprint (git root hash, canonical path, or repo identity) "
            "must be incorporated."
        )

    def test_project_local_storage_has_fingerprint_verification(self):
        """Bug E: ProjectLocalStorage must expose a method to verify that
        the .memory-engine/ database belongs to the current project root.
        Without this, copying .memory-engine/ to another project silently
        inherits the original project's memories."""
        from memory_engine.bootstrap.local_storage import ProjectLocalStorage

        storage = ProjectLocalStorage(Path("."))
        has_verification = (
            hasattr(storage, "verify_project_fingerprint")
            or hasattr(storage, "is_bound_to")
            or hasattr(storage, "project_fingerprint")
            or hasattr(storage, "get_fingerprint")
        )
        assert has_verification, (
            "Bug E: ProjectLocalStorage has no fingerprint verification method. "
            "Copying .memory-engine/ to a different project silently inherits memories. "
            "At minimum, a stored fingerprint (canonical git root, project path hash) "
            "must be written on first bind and verified on subsequent opens."
        )

    def test_ensure_project_uses_fingerprint_not_just_name(self):
        """Bug E: ProjectBootstrapService._ensure_project() must use a fingerprint
        beyond name= lookup. The fingerprint or canonical path must appear in the
        project-lookup query."""
        import inspect
        from memory_engine.bootstrap import bootstrap_service as bs_module
        source = inspect.getsource(bs_module.ProjectBootstrapService._ensure_project)

        # Bug E: only basename name= is used. After fix: fingerprint or canonical path
        uses_fingerprint_in_lookup = (
            "fingerprint" in source
            or "canonical_path" in source
        )
        assert uses_fingerprint_in_lookup, (
            "Bug E: ProjectBootstrapService._ensure_project() looks up project by "
            "filter_by(name=...) — basename only. Two 'backend' directories at "
            "different paths would share the same project row. Fix: add fingerprint "
            "column to ProjectORM and use it in the lookup."
        )


# ---------------------------------------------------------------------------
# Bug F: SimpleCache.make_key() has no memory_generation parameter
# ---------------------------------------------------------------------------

class TestBugF_CacheGenerationKey:
    """
    SimpleCache.make_key() includes branch, commit, and working_tree_dirty.
    It does NOT include a memory_generation counter. If memories are
    added/modified without changing branch or commit, the cache can return
    stale results even after new memories are written.
    """

    def test_cache_key_accepts_memory_generation(self):
        """Bug F: SimpleCache.make_key() must accept a memory_generation
        parameter so the cache can be invalidated when memories are written."""
        from memory_engine.knowledge.cache import SimpleCache
        import inspect
        sig = inspect.signature(SimpleCache.make_key)
        assert "memory_generation" in sig.parameters, (
            "Bug F: SimpleCache.make_key() has no 'memory_generation' parameter. "
            "Writing new memories without changing branch or commit cannot "
            "invalidate the cache, potentially serving stale results."
        )

    def test_different_memory_generations_yield_different_keys(self):
        """Bug F: keys with different memory_generation must be different."""
        from memory_engine.knowledge.cache import SimpleCache
        project_id = str(uuid.uuid4())
        base_kwargs = dict(
            project_id=project_id,
            normalized_query="authentication approach",
            current_files=[],
            current_symbols=[],
            token_budget=4000,
            current_branch="main",
            head_commit="abc123",
        )
        key_gen0 = SimpleCache.make_key(**base_kwargs, memory_generation=0)  # type: ignore[call-arg]
        key_gen1 = SimpleCache.make_key(**base_kwargs, memory_generation=1)  # type: ignore[call-arg]
        assert key_gen0 != key_gen1, (
            "Bug F: different memory_generation values must produce different cache keys "
            "so that new memory writes cause cache misses."
        )


# ---------------------------------------------------------------------------
# Bug G: Stale/superseded memories returned as selected active context
# ---------------------------------------------------------------------------

class TestBugG_StaleMemoryInContext:
    """
    MemoryStatus.stale and MemoryStatus.superseded exist in the model.
    The ContextComposer must exclude stale/superseded nodes from 'selected'
    buckets, showing them only as 'excluded' in the retrieval trace.
    """

    def _set_status(self, session: Session, node_id: str, status: str) -> None:
        """Directly update node status via ORM."""
        repo = MemoryNodeRepository(session)
        repo.update_status(node_id, status)

    def test_stale_memory_not_selected(
        self, project_service, memory_service, recall_service, session
    ):
        """A 'stale' memory must never appear as action='selected'."""
        proj = project_service.create(
            ProjectCreate(name="stale-test-project", description="")
        )
        project_uuid = proj.id

        stale_node = memory_service.create_node(MemoryNodeCreate(
            project_id=project_uuid,
            title="Authentication approach",
            summary="Use OAuth2 with PKCE for all authentication flows.",
            kind=MemoryKind.architecture,
            importance=0.95,
            confidence=0.95,
            tags=["auth", "oauth2", "pkce"],
        ))
        stale_id = str(stale_node.id)
        self._set_status(session, stale_id, "stale")

        req = RecallRequest(
            project_id=project_uuid,
            current_task="How should the authentication system work?",
        )
        result = recall_service.recall(req)

        selected_stale = [
            te for te in result.retrieval_trace
            if te.memory_id == stale_id and te.action == "selected"
        ]
        assert len(selected_stale) == 0, (
            f"Bug G: stale memory '{stale_id}' appeared as 'selected' in context. "
            "Stale memories must be excluded from active authoritative context. "
            "They may appear in trace as 'excluded' but must not be injected as instructions."
        )

    def test_superseded_memory_not_selected(
        self, project_service, memory_service, recall_service, session
    ):
        """A 'superseded' memory must not appear as 'selected'."""
        proj = project_service.create(
            ProjectCreate(name="superseded-project", description="")
        )
        project_uuid = proj.id

        old_node = memory_service.create_node(MemoryNodeCreate(
            project_id=project_uuid,
            title="Deployment strategy",
            summary="Deploy using Heroku containers.",
            kind=MemoryKind.decision,
            importance=0.9,
            confidence=0.9,
            tags=["deploy", "heroku"],
        ))
        old_id = str(old_node.id)
        self._set_status(session, old_id, "superseded")

        active_node = memory_service.create_node(MemoryNodeCreate(
            project_id=project_uuid,
            title="Deployment strategy",
            summary="Deploy using Kubernetes on GKE. Heroku approach deprecated.",
            kind=MemoryKind.decision,
            importance=0.95,
            confidence=0.95,
            tags=["deploy", "kubernetes", "gke"],
        ))
        active_id = str(active_node.id)

        req = RecallRequest(
            project_id=project_uuid,
            current_task="How do we deploy our services?",
        )
        result = recall_service.recall(req)

        superseded_selected = [
            te for te in result.retrieval_trace
            if te.memory_id == old_id and te.action == "selected"
        ]
        assert len(superseded_selected) == 0, (
            "Bug G: superseded Heroku deployment memory appeared as 'selected'. "
            "The newer Kubernetes decision should take precedence."
        )

        # The active decision should be selected
        active_selected = [
            te for te in result.retrieval_trace
            if te.memory_id == active_id and te.action == "selected"
        ]
        assert len(active_selected) >= 1, (
            "The active Kubernetes deployment decision must be selected."
        )
