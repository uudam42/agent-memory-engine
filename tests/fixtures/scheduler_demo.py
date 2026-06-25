"""Scheduler demo fixture — populates a realistic memory tree for Stage 2 tests.

Tree:
  scheduler-system (project)
  ├── Scheduler Architecture           [architecture]  importance=0.9
  │   ├── Task Lifecycle State Machine [module]        importance=0.95  module_path=scheduler.lifecycle
  │   ├── Retry Policy                 [module]        importance=0.85  module_path=scheduler.retry
  │   └── Execution Runtime            [module]        importance=0.80  module_path=scheduler.execution
  ├── Decisions
  │   └── Explicit terminal state transitions  [decision]  importance=0.90
  ├── Incidents
  │   └── Missing terminal state update causes indefinite waiting  [debug]  importance=0.92  status=stale
  └── Procedures
      └── Run scheduler state transition tests  [procedure]  importance=0.75

Relations:
  - Task Lifecycle → depends_on → Explicit terminal state decision
  - Incident → related_to → Task Lifecycle
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.models.domain import (
    EvidenceCreate,
    MemoryKind,
    MemoryNodeCreate,
    MemoryRelationCreate,
    MemoryStatus,
    ProjectCreate,
    RelationType,
)
from memory_engine.repositories.relation import RelationRepository
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService


def create_scheduler_demo(session: Session) -> dict:
    """Create the scheduler demo project and return a dict of named objects."""
    p_svc = ProjectService(session)
    m_svc = MemoryService(session)
    r_repo = RelationRepository(session)

    project = p_svc.create(
        ProjectCreate(
            name="scheduler-system",
            description="A task scheduler with lifecycle management and retry support.",
        )
    )

    # ── Root architecture ───────────────────────────────────────────────
    arch = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Scheduler Architecture",
        summary=(
            "The scheduler manages task queuing, execution, and lifecycle transitions. "
            "All tasks must transition through defined lifecycle states."
        ),
        kind=MemoryKind.architecture,
        importance=0.9,
        confidence=0.95,
        tags=["scheduler", "architecture", "lifecycle"],
    ))

    # ── Architecture children ───────────────────────────────────────────
    lifecycle = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        parent_id=arch.id,
        title="Task Lifecycle State Machine",
        summary=(
            "Tasks follow a strict state machine: PENDING → RUNNING → (SUCCEEDED | FAILED | CANCELLED). "
            "A task must always transition from RUNNING to a terminal state before the scheduler "
            "releases the execution slot. Skipping the terminal transition causes indefinite slot starvation."
        ),
        kind=MemoryKind.module,
        importance=0.95,
        confidence=0.95,
        module_path="scheduler.lifecycle",
        tags=["state_machine", "lifecycle", "terminal_state", "scheduler"],
    ))

    retry_policy = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        parent_id=arch.id,
        title="Retry Policy",
        summary=(
            "Tasks may be retried up to max_retries times after FAILED. "
            "Each retry resets the task to PENDING and re-enqueues it. "
            "The retry counter must be checked before re-enqueueing to avoid infinite loops."
        ),
        kind=MemoryKind.module,
        importance=0.85,
        confidence=0.90,
        module_path="scheduler.retry",
        tags=["retry", "backoff", "scheduler", "max_retries"],
    ))

    runtime = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        parent_id=arch.id,
        title="Execution Runtime",
        summary=(
            "The execution runtime allocates worker slots and dispatches tasks. "
            "It listens for state-transition events to update internal counters."
        ),
        kind=MemoryKind.module,
        importance=0.80,
        confidence=0.90,
        module_path="scheduler.execution",
        tags=["execution", "worker", "slot", "scheduler"],
    ))

    # ── Decisions ───────────────────────────────────────────────────────
    terminal_decision = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Explicit terminal state transitions required",
        summary=(
            "Decision (2024-03): All state-machine transitions to terminal states "
            "(SUCCEEDED, FAILED, CANCELLED) must be explicit and atomic. "
            "Implicit completion on task function return is forbidden because it races "
            "with timeout and cancellation signals."
        ),
        kind=MemoryKind.decision,
        importance=0.90,
        confidence=0.95,
        tags=["decision", "state_machine", "terminal_state", "atomic"],
    ))

    # ── Constraints ─────────────────────────────────────────────────────
    constraint = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Terminal state transitions must be atomic",
        summary=(
            "CONSTRAINT: The scheduler must never leave a task in RUNNING state "
            "after the execution function completes or raises. "
            "Any new retry, cancel, or timeout path must explicitly call "
            "transition_to_terminal() before releasing the worker slot."
        ),
        kind=MemoryKind.constraint,
        importance=1.0,
        confidence=1.0,
        tags=["constraint", "state_machine", "terminal_state", "retry", "lifecycle"],
    ))

    # ── Incidents ───────────────────────────────────────────────────────
    incident = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Missing terminal state update causes indefinite slot starvation",
        summary=(
            "Incident (2024-02): A task that raised an unhandled exception in the retry "
            "path was left in RUNNING state. The execution slot was never freed. "
            "The scheduler deadlocked after all slots were exhausted by zombie tasks. "
            "Root cause: retry handler called re-enqueue without calling transition_to_terminal first."
        ),
        kind=MemoryKind.debug,
        importance=0.92,
        confidence=0.98,
        status=MemoryStatus.active,   # active — still relevant warning
        tags=["incident", "bug", "state_machine", "retry", "starvation", "terminal_state"],
    ))

    # Add evidence to the incident
    m_svc.add_evidence(EvidenceCreate(
        memory_node_id=incident.id,
        content=(
            "PR #142 introduced retry logic without calling transition_to_terminal(). "
            "Fix in PR #155: added explicit terminal transition before re-enqueue."
        ),
        source="github.com/org/scheduler/pull/155",
    ))

    # ── Procedures ──────────────────────────────────────────────────────
    test_procedure = m_svc.create_node(MemoryNodeCreate(
        project_id=project.id,
        title="Run scheduler state transition tests before merging",
        summary=(
            "Before merging any change that touches task lifecycle, retry logic, or "
            "execution runtime: run pytest tests/scheduler/test_state_machine.py -v. "
            "All transition matrix tests must pass. Check slot-count invariants."
        ),
        kind=MemoryKind.procedure,
        importance=0.75,
        confidence=0.90,
        tags=["procedure", "testing", "state_machine", "scheduler"],
    ))

    # ── Relations ───────────────────────────────────────────────────────
    # Task Lifecycle depends on the terminal-state decision
    r_repo.create(
        source_id=str(lifecycle.id),
        target_id=str(terminal_decision.id),
        relation_type=RelationType.depends_on.value,
    )
    # Incident is related to Task Lifecycle
    r_repo.create(
        source_id=str(incident.id),
        target_id=str(lifecycle.id),
        relation_type=RelationType.related_to.value,
    )
    # Constraint implements the terminal-state decision
    r_repo.create(
        source_id=str(constraint.id),
        target_id=str(terminal_decision.id),
        relation_type=RelationType.implements.value,
    )

    return {
        "project": project,
        "arch": arch,
        "lifecycle": lifecycle,
        "retry_policy": retry_policy,
        "runtime": runtime,
        "terminal_decision": terminal_decision,
        "constraint": constraint,
        "incident": incident,
        "test_procedure": test_procedure,
    }
