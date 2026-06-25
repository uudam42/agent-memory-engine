"""Scheduler knowledge-base demo fixtures for Phase 6 tests.

Creates 7 knowledge documents:
  1. README
  2. Scheduler architecture document
  3. Retry Policy ADR
  4. Task lifecycle module source file
  5. Retry module source file
  6. State transition test report
  7. Historical PR / diff artifact (terminal-state bug)
"""

from __future__ import annotations

README = """\
# Scheduler Service

## Overview

The Scheduler Service manages long-running tasks with retry, timeout, and
lifecycle state management.

## Running the Service

```bash
python -m scheduler.server --port 8080
```

## Running Tests

```bash
pytest tests/scheduler/ -v
```

## Environment Variables

- `SCHEDULER_MAX_RETRIES`: maximum retry attempts (default: 3)
- `SCHEDULER_RETRY_BACKOFF`: backoff factor in seconds (default: 2.0)
- `SCHEDULER_TIMEOUT`: per-task timeout in seconds (default: 300)

## Architecture

See `docs/architecture.md` for the full architecture document.
The retry policy ADR is in `docs/adr/001-retry-policy.md`.
"""

ARCHITECTURE_DOC = """\
# Scheduler Architecture

## Overview

The scheduler is built around a state-machine that manages task lifecycle.

## Core Components

### Task Lifecycle (scheduler/lifecycle.py)

All tasks move through a finite set of states:
- `pending` → waiting to be picked up
- `running` → actively executing
- `retry_pending` → awaiting retry after failure
- `terminal` → complete (succeeded or permanently failed)

The lifecycle module exposes `transition_to_terminal(task_id, reason)`.
This function MUST be called before any re-enqueue operation.

### Retry Coordinator (scheduler/retry.py)

The RetryCoordinator manages retry decisions:
- Checks remaining retry budget
- Applies exponential backoff
- Calls `transition_to_terminal()` before re-enqueue (critical invariant)

### State Machine Constraints

- A task in terminal state CANNOT be re-enqueued.
- `transition_to_terminal()` is the single authoritative path to terminal state.
- All retry paths MUST call `transition_to_terminal()` before `re_enqueue()`.
- Slot reservation is released atomically with the terminal transition.

## Data Flow

```
Task arrives → pending
  → scheduler picks up → running
  → task fails → RetryCoordinator.decide()
    → budget available: transition_to_terminal() → re_enqueue() → retry_pending → running
    → budget exhausted: transition_to_terminal() → terminal (failed)
  → task succeeds: transition_to_terminal() → terminal (succeeded)
```

## Failure Modes

### Slot Starvation (Historical Bug, Fixed)

In v1.2 (pre-fix), the retry handler called `re_enqueue()` WITHOUT first
calling `transition_to_terminal()`. This caused:
- The task to remain in `running` state while also being re-enqueued.
- Slot reservation was never released.
- Under load, all slots were consumed → starvation.

Fixed in v1.3 by requiring `transition_to_terminal()` before any re-enqueue.
"""

RETRY_POLICY_ADR = """\
# ADR 001 — Retry Policy for the Scheduler Service

**Status:** Accepted
**Date:** 2025-01-15
**Authors:** Platform Team

## Context

The scheduler needs a consistent retry policy that:
1. Prevents indefinite retry loops.
2. Avoids slot starvation.
3. Provides observable retry state.

## Decision

We adopt exponential backoff with jitter and a maximum retry budget.

### Parameters

- `max_retries`: 3 (configurable via env)
- `base_backoff`: 2.0 seconds
- `jitter`: uniform random in [0, base_backoff)
- `backoff(n)`: base_backoff * 2^n + jitter

### Invariant

**CRITICAL:** `transition_to_terminal(task_id)` MUST be called before any
`re_enqueue()` call.  This releases the slot and prevents starvation.

## Alternatives Considered

### Linear Backoff

Rejected: Does not provide enough spread under burst load.

### Immediate Retry

Rejected: Caused slot exhaustion in load tests (see incident-2024-11-30).

## Consequences

- All retry paths must route through `RetryCoordinator.handle_failure()`.
- The lifecycle module is the single source of truth for terminal state.
- Tests must cover the terminal-transition-before-re-enqueue invariant.
"""

LIFECYCLE_SOURCE = """\
\"\"\"scheduler/lifecycle.py — Task lifecycle state management.\"\"\"

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class TaskState(StrEnum):
    pending = "pending"
    running = "running"
    retry_pending = "retry_pending"
    terminal = "terminal"


@dataclass
class Task:
    task_id: str
    state: TaskState = TaskState.pending
    retry_count: int = 0
    slot_id: Optional[str] = None


_TASKS: dict[str, Task] = {}


def get_task(task_id: str) -> Task:
    return _TASKS[task_id]


def transition_to_terminal(task_id: str, reason: str = "completed") -> Task:
    \"\"\"Transition a task to terminal state and release its slot.

    MUST be called before any re_enqueue() operation.
    This is the single authoritative path to terminal state.
    \"\"\"
    task = get_task(task_id)
    if task.state == TaskState.terminal:
        raise ValueError(f"Task {task_id!r} is already in terminal state")
    task.state = TaskState.terminal
    if task.slot_id:
        _release_slot(task.slot_id)
        task.slot_id = None
    return task


def re_enqueue(task_id: str) -> Task:
    \"\"\"Re-enqueue a task for retry.

    The task MUST be in terminal state before calling this.
    (Call transition_to_terminal() first.)
    \"\"\"
    task = get_task(task_id)
    if task.state != TaskState.terminal:
        raise ValueError(
            f"Cannot re-enqueue task {task_id!r} in state {task.state!r}. "
            f"Call transition_to_terminal() first."
        )
    task.state = TaskState.retry_pending
    task.retry_count += 1
    return task


def _release_slot(slot_id: str) -> None:
    \"\"\"Internal: release a scheduler slot reservation.\"\"\"
    pass  # implementation in slot_manager.py
"""

RETRY_SOURCE = """\
\"\"\"scheduler/retry.py — RetryCoordinator for exponential backoff retry logic.\"\"\"

from __future__ import annotations

import random
import time
from dataclasses import dataclass


@dataclass
class RetryDecision:
    should_retry: bool
    backoff_seconds: float
    reason: str


class RetryCoordinator:
    \"\"\"Decides whether and when to retry a failed task.

    Invariant: handle_failure() ALWAYS calls transition_to_terminal() before
    calling re_enqueue().  This is the critical constraint that prevents slot
    starvation.
    \"\"\"

    def __init__(
        self,
        max_retries: int = 3,
        base_backoff: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_backoff = base_backoff

    def decide(self, retry_count: int) -> RetryDecision:
        \"\"\"Decide whether to retry based on current retry count.\"\"\"
        if retry_count >= self.max_retries:
            return RetryDecision(
                should_retry=False,
                backoff_seconds=0.0,
                reason=f"max_retries ({self.max_retries}) exhausted",
            )
        backoff = self.base_backoff * (2 ** retry_count)
        jitter = random.uniform(0, self.base_backoff)
        return RetryDecision(
            should_retry=True,
            backoff_seconds=backoff + jitter,
            reason=f"retry {retry_count + 1} of {self.max_retries}",
        )

    def handle_failure(self, task_id: str, retry_count: int) -> RetryDecision:
        \"\"\"Handle a task failure: decide retry, call terminal transition first.\"\"\"
        from scheduler.lifecycle import re_enqueue, transition_to_terminal

        decision = self.decide(retry_count)

        # CRITICAL: transition_to_terminal() MUST come before re_enqueue()
        transition_to_terminal(task_id, reason="retry_handoff")

        if decision.should_retry:
            time.sleep(decision.backoff_seconds)
            re_enqueue(task_id)

        return decision
"""

STATE_MACHINE_TEST_REPORT = """\
Test Suite: scheduler/test_state_machine.py
Run at: 2025-01-20 14:32:01 UTC
Python: 3.12.0
Pytest: 8.2.0

============================================================
PASSED test_task_starts_in_pending_state
PASSED test_transition_to_running_from_pending
PASSED test_transition_to_terminal_releases_slot
PASSED test_cannot_reenqueue_without_terminal_transition
FAILED test_exponential_backoff_respects_max_retries - AssertionError: expected backoff 4.0, got 3.8
PASSED test_retry_coordinator_calls_terminal_before_reenqueue
PASSED test_slot_not_released_on_direct_reenqueue_without_terminal
PASSED test_terminal_state_is_idempotent_check

============================================================
FAILURES
============================================================

FAILED test_exponential_backoff_respects_max_retries
  tests/scheduler/test_state_machine.py:88
  AssertionError: expected backoff 4.0, got 3.8
  (jitter caused off-by-one — test was using exact equality; fixed by using >= check)

============================================================
8 tests, 7 passed, 1 failed
Duration: 0.43s
============================================================

IMPORTANT: test_retry_coordinator_calls_terminal_before_reenqueue is the
critical regression test for the slot-starvation incident.  MUST pass.
"""

TERMINAL_BUG_DIFF = """\
diff --git a/scheduler/retry.py b/scheduler/retry.py
index 4a1b2c3..9f8d7e2 100644
--- a/scheduler/retry.py
+++ b/scheduler/retry.py
@@ -42,8 +42,12 @@ class RetryCoordinator:
     def handle_failure(self, task_id: str, retry_count: int) -> RetryDecision:
         '''Handle a task failure: decide retry, then re-enqueue if appropriate.'''
-        from scheduler.lifecycle import re_enqueue
+        from scheduler.lifecycle import re_enqueue, transition_to_terminal
         decision = self.decide(retry_count)
+
+        # Fix: transition_to_terminal() MUST be called before re_enqueue()
+        # Previously missing — caused slot starvation under load.
+        transition_to_terminal(task_id, reason='retry_handoff')
+
         if decision.should_retry:
             time.sleep(decision.backoff_seconds)
             re_enqueue(task_id)

diff --git a/tests/scheduler/test_state_machine.py b/tests/scheduler/test_state_machine.py
index 1c2d3e4..5f6a7b8 100644
--- a/tests/scheduler/test_state_machine.py
+++ b/tests/scheduler/test_state_machine.py
@@ -80,0 +81,10 @@
+def test_retry_coordinator_calls_terminal_before_reenqueue():
+    '''Regression test: RetryCoordinator must call transition_to_terminal()
+    before re_enqueue() to prevent slot starvation.'''
+    task = make_task()
+    coordinator = RetryCoordinator(max_retries=3)
+    with patch('scheduler.lifecycle.transition_to_terminal') as mock_terminal:
+        coordinator.handle_failure(task.task_id, retry_count=0)
+        mock_terminal.assert_called_once_with(task.task_id, reason='retry_handoff')
"""


def build_knowledge_demo_documents() -> list[dict]:
    """Return list of document dicts ready for KnowledgeIngestRequest."""
    return [
        {
            "source_type": "readme",
            "title": "Scheduler Service README",
            "content": README,
            "source_path": "README.md",
        },
        {
            "source_type": "architecture_doc",
            "title": "Scheduler Architecture",
            "content": ARCHITECTURE_DOC,
            "source_path": "docs/architecture.md",
        },
        {
            "source_type": "adr",
            "title": "ADR 001 — Retry Policy",
            "content": RETRY_POLICY_ADR,
            "source_path": "docs/adr/001-retry-policy.md",
            "tags": ["retry", "backoff", "adr"],
        },
        {
            "source_type": "code_file",
            "title": "Task Lifecycle Module",
            "content": LIFECYCLE_SOURCE,
            "source_path": "scheduler/lifecycle.py",
            "tags": ["lifecycle", "state-machine"],
        },
        {
            "source_type": "code_file",
            "title": "Retry Coordinator Module",
            "content": RETRY_SOURCE,
            "source_path": "scheduler/retry.py",
            "tags": ["retry", "backoff"],
        },
        {
            "source_type": "test_report",
            "title": "State Machine Test Report",
            "content": STATE_MACHINE_TEST_REPORT,
            "source_path": "tests/scheduler/test_state_machine.py",
            "tags": ["test-report", "state-machine"],
        },
        {
            "source_type": "git_diff",
            "title": "PR: Fix slot starvation — terminal transition before re-enqueue",
            "content": TERMINAL_BUG_DIFF,
            "source_path": ".git/DIFF_FIX_SLOT_STARVATION",
            "tags": ["fix", "slot-starvation", "terminal-state"],
        },
    ]
