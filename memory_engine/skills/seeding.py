"""ProjectSeedingService — create initial memory nodes from structured project input.

Solves the cold-start problem: new projects have zero memory nodes, so
retrieve_agent_context returns nothing useful for the first several sessions.

Seeding bypasses the candidate/promotion pipeline and writes nodes directly
to active status, because the information comes from an authoritative human
source (the user describing their own project) rather than inferred from code.

Auto-extraction also attempts to parse README.md for constraint/architecture
sections so that zero-input seeding produces something useful out of the box.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from memory_engine.models.domain import MemoryKind, MemoryNodeCreate
from memory_engine.models.orm import ProjectORM
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService


@dataclass
class SeedInput:
    project_id: uuid.UUID
    project_root: Path
    description: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    tech_stack: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    # if True, skip auto-extraction from README even if description is empty
    skip_auto_extract: bool = False


@dataclass
class SeedResult:
    nodes_created: int
    module_nodes: int
    constraint_nodes: int
    decision_nodes: int
    procedure_nodes: int
    skipped_reason: str | None = None
    node_titles: list[str] = field(default_factory=list)


# Headings that suggest constraint content in a README
_CONSTRAINT_HEADINGS = re.compile(
    r"^#{1,3}\s+.*(constraint|rule|limit|must|must not|forbidden|never|"
    r"invariant|security|auth|principle|requirement|policy).*$",
    re.I | re.M,
)
_DECISION_HEADINGS = re.compile(
    r"^#{1,3}\s+.*(decision|architecture|design|adr|rationale|why|chose|choice).*$",
    re.I | re.M,
)

# Minimum word count to treat a README description as meaningful
_MIN_DESC_WORDS = 6


def _extract_bullets(text: str) -> list[str]:
    """Extract non-empty bullet list items from markdown text."""
    bullets = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*+]\s+(.+)", line)
        if m:
            item = m.group(1).strip()
            if len(item.split()) >= 3:  # skip single-word bullets
                bullets.append(item)
    return bullets


def _section_text_after_heading(markdown: str, heading_re: re.Pattern) -> list[str]:
    """Return bullet items from sections whose heading matches heading_re."""
    results: list[str] = []
    lines = markdown.splitlines()
    in_section = False
    for line in lines:
        if re.match(r"^#{1,3}\s+", line):
            in_section = bool(heading_re.match(line))
        elif in_section:
            m = re.match(r"^\s*[-*+]\s+(.+)", line)
            if m:
                item = m.group(1).strip()
                if len(item.split()) >= 3:
                    results.append(item)
    return results


def _readme_description(readme: str) -> str:
    """Extract a short project description from README intro paragraph."""
    lines = readme.splitlines()
    # Skip title line, find first non-empty non-heading paragraph
    past_title = False
    para: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not past_title:
            if re.match(r"^#\s+", line):
                past_title = True
            continue
        if re.match(r"^#{1,3}\s+", line):
            break
        if stripped:
            para.append(stripped)
            if len(" ".join(para).split()) >= 20:
                break
        elif para:
            break
    return " ".join(para)[:400]


class ProjectSeedingService:
    """Write initial memory nodes directly from user-provided project context.

    Nodes are written as active with confidence=1.0 because the source is an
    authoritative human description, not inferred from code.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._memory_svc = MemoryService(session)

    def seed(self, inp: SeedInput) -> SeedResult:
        """Create initial memory nodes from inp. Idempotent within a run."""
        constraints = list(inp.constraints)
        decisions = list(inp.decisions)
        conventions = list(inp.conventions)
        description = inp.description.strip()

        # Auto-extract from README if caller hasn't provided enough content
        if not inp.skip_auto_extract:
            readme = self._read_readme(inp.project_root)
            if readme:
                if not description and len(readme.split()) >= _MIN_DESC_WORDS:
                    description = _readme_description(readme)
                if not constraints:
                    constraints = _section_text_after_heading(readme, _CONSTRAINT_HEADINGS)[:10]
                if not decisions:
                    decisions = _section_text_after_heading(readme, _DECISION_HEADINGS)[:10]

        # Require at least a description to proceed
        if not description and not constraints and not decisions and not conventions:
            return SeedResult(
                nodes_created=0,
                module_nodes=0,
                constraint_nodes=0,
                decision_nodes=0,
                procedure_nodes=0,
                skipped_reason=(
                    "No content to seed: provide description, constraints, decisions, "
                    "or conventions — or ensure README.md exists with project content."
                ),
            )

        created: list[str] = []
        module_n = constraint_n = decision_n = procedure_n = 0

        # 1. Root module node — project overview
        if description:
            title = f"{inp.project_root.name} — project overview"
            tech_note = (
                f" Tech stack: {', '.join(inp.tech_stack[:6])}." if inp.tech_stack else ""
            )
            node = self._memory_svc.create_node(MemoryNodeCreate(
                project_id=inp.project_id,
                title=title,
                summary=description + tech_note,
                kind=MemoryKind.module,
                importance=0.75,
                confidence=1.0,
                tags=["project_overview", "seed"] + inp.tech_stack[:4],
            ))
            created.append(node.title)
            module_n += 1

        # 2. Constraint nodes
        for text in constraints[:12]:
            node = self._memory_svc.create_node(MemoryNodeCreate(
                project_id=inp.project_id,
                title=self._short_title(text, prefix="Constraint"),
                summary=text,
                kind=MemoryKind.constraint,
                importance=0.90,
                confidence=1.0,
                tags=["constraint", "seed"],
            ))
            created.append(node.title)
            constraint_n += 1

        # 3. Decision nodes
        for text in decisions[:8]:
            node = self._memory_svc.create_node(MemoryNodeCreate(
                project_id=inp.project_id,
                title=self._short_title(text, prefix="Decision"),
                summary=text,
                kind=MemoryKind.decision,
                importance=0.82,
                confidence=1.0,
                tags=["decision", "architecture", "seed"],
            ))
            created.append(node.title)
            decision_n += 1

        # 4. Procedure node — team conventions (bundle into one node)
        if conventions:
            summary = "\n".join(f"- {c}" for c in conventions[:10])
            node = self._memory_svc.create_node(MemoryNodeCreate(
                project_id=inp.project_id,
                title="Team conventions and workflow rules",
                summary=summary,
                kind=MemoryKind.procedure,
                importance=0.70,
                confidence=1.0,
                tags=["convention", "workflow", "seed"],
            ))
            created.append(node.title)
            procedure_n += 1

        total = module_n + constraint_n + decision_n + procedure_n
        return SeedResult(
            nodes_created=total,
            module_nodes=module_n,
            constraint_nodes=constraint_n,
            decision_nodes=decision_n,
            procedure_nodes=procedure_n,
            node_titles=created,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_readme(self, project_root: Path) -> str:
        for name in ("README.md", "README.rst", "README.txt", "readme.md"):
            p = project_root / name
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8", errors="replace")[:8000]
                except Exception:
                    pass
        return ""

    def _short_title(self, text: str, prefix: str) -> str:
        first = re.split(r"[.!?\n]", text.strip())[0]
        return f"{prefix}: {first[:72].rstrip()}"
