"""Typer CLI — `memory` command group.

Normal usage is via the coding agent:
  Agent → SkillRouter → Recall / Inspect → Memory Engine service

Phase 3 direct commands (human-facing for curation):
  memory promote <candidate_id>
  memory stale   <node_id> --reason "..."
  memory relations <node_id>
  memory inspect <project_name> <node_id>   (full inspect with evidence)

The `debug` subgroup is for development / testing only.
"""

from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

import typer
from rich import print as rprint
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from memory_engine.db.init_db import init_db
from memory_engine.db.session import SessionLocal
from memory_engine.models.domain import (
    CandidateCreate,
    EvidenceCreate,
    MarkStaleRequest,
    MemoryKind,
    MemoryNodeCreate,
    ProjectCreate,
    RiskLevel,
    RouteRequest,
)
from memory_engine.services.memory_service import (
    DepthLimitExceededError,
    MemoryNodeNotFoundError,
    MemoryService,
    ProjectNotFoundError,
)
from memory_engine.services.project_service import ProjectAlreadyExistsError, ProjectService
from memory_engine.services.promotion import CandidateNotFoundError, PromotionService
from memory_engine.services.tree_service import TreeService
from memory_engine.skills.inspect import InspectService
from memory_engine.skills.recall import RecallService
from memory_engine.skills.router import SkillRouter

app = typer.Typer(name="memory", help="Memory Engine CLI", no_args_is_help=True)
project_app = typer.Typer(help="Manage projects", no_args_is_help=True)
evidence_app = typer.Typer(help="Manage evidence", no_args_is_help=True)
debug_app = typer.Typer(
    help="Debug commands — not the normal agent workflow",
    no_args_is_help=True,
)
app.add_typer(project_app, name="project")
app.add_typer(evidence_app, name="evidence")
app.add_typer(debug_app, name="debug")

console = Console()


def _get_session():  # type: ignore[no-untyped-def]
    return SessionLocal()


def _find_project(name: str):  # type: ignore[no-untyped-def]
    with _get_session() as session:
        svc = ProjectService(session)
        projects = svc.list_all()
    match = next((p for p in projects if p.name == name), None)
    if match is None:
        rprint(f"[red]Project '{name}' not found.[/red]")
        raise typer.Exit(1)
    return match


# ---------------------------------------------------------------------------
# memory init
# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Initialise the database (create tables)."""
    init_db()
    rprint("[green]✓[/green] Database initialised.")


# ---------------------------------------------------------------------------
# memory project list
# ---------------------------------------------------------------------------


@project_app.command("list")
def project_list() -> None:
    """List all projects."""
    with _get_session() as session:
        svc = ProjectService(session)
        projects = svc.list_all()

    if not projects:
        rprint("[yellow]No projects found.[/yellow]")
        return

    table = Table(title="Projects", show_header=True)
    table.add_column("ID", style="dim")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Created", style="dim")
    for p in projects:
        table.add_row(
            str(p.id), p.name, p.description or "",
            p.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# memory tree
# ---------------------------------------------------------------------------


@app.command()
def tree(project_name: str = typer.Argument(..., help="Project name")) -> None:
    """Display the memory tree for a project."""
    project = _find_project(project_name)
    with _get_session() as session:
        t_svc = TreeService(session)
        text = t_svc.render_text(str(project.id))
    rprint(f"[bold]{project_name}[/bold]")
    rprint(text if text else "[dim](empty)[/dim]")


# ---------------------------------------------------------------------------
# memory add
# ---------------------------------------------------------------------------


@app.command()
def add(
    project_name: str = typer.Argument(..., help="Project name"),
    title: str = typer.Option(..., "--title", "-t"),
    summary: str = typer.Option(..., "--summary", "-s"),
    kind: MemoryKind = typer.Option(MemoryKind.module, "--kind", "-k"),
    parent_id: Optional[str] = typer.Option(None, "--parent", "-p"),
    tags: Optional[str] = typer.Option(None, "--tags"),
    importance: float = typer.Option(0.5, "--importance"),
    confidence: float = typer.Option(1.0, "--confidence"),
    module_path: Optional[str] = typer.Option(None, "--module-path"),
) -> None:
    """Add a memory node directly (bypasses promotion pipeline)."""
    project = _find_project(project_name)
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    payload = MemoryNodeCreate(
        project_id=project.id,
        parent_id=UUID(parent_id) if parent_id else None,
        title=title, summary=summary, kind=kind, tags=tag_list,
        importance=importance, confidence=confidence, module_path=module_path,
    )
    with _get_session() as session:
        m_svc = MemoryService(session)
        try:
            node = m_svc.create_node(payload)
        except (ProjectNotFoundError, MemoryNodeNotFoundError, DepthLimitExceededError) as exc:
            rprint(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc
    rprint(f"[green]✓[/green] Created node [bold]{node.id}[/bold]: {node.title}")


# ---------------------------------------------------------------------------
# memory evidence add
# ---------------------------------------------------------------------------


@evidence_app.command("add")
def evidence_add(
    node_id: str = typer.Argument(..., help="Memory node ID"),
    content: str = typer.Option(..., "--content", "-c"),
    source: Optional[str] = typer.Option(None, "--source"),
) -> None:
    """Attach evidence to a memory node."""
    payload = EvidenceCreate(memory_node_id=UUID(node_id), content=content, source=source)
    with _get_session() as session:
        m_svc = MemoryService(session)
        try:
            ev = m_svc.add_evidence(payload)
        except MemoryNodeNotFoundError as exc:
            rprint(f"[red]Node not found:[/red] {exc}")
            raise typer.Exit(1) from exc
    rprint(f"[green]✓[/green] Evidence [bold]{ev.id}[/bold] attached.")


# ---------------------------------------------------------------------------
# Phase 3 — memory promote
# ---------------------------------------------------------------------------


@app.command()
def promote(
    candidate_id: str = typer.Argument(..., help="Candidate UUID to promote"),
) -> None:
    """Run the promotion pipeline for a staged candidate.

    The system will classify the candidate, check for duplicates and conflicts,
    and either create, update, supersede, merge, discard, or flag for review.
    """
    with _get_session() as session:
        svc = PromotionService(session)
        try:
            result = svc.promote(candidate_id)
        except CandidateNotFoundError:
            rprint(f"[red]Candidate not found:[/red] {candidate_id}")
            raise typer.Exit(1)

    icon = {
        "create": "[green]✓ CREATED[/green]",
        "supersede": "[blue]⇑ SUPERSEDED[/blue]",
        "update": "[cyan]↑ UPDATED[/cyan]",
        "merge": "[cyan]⊕ MERGED[/cyan]",
        "discard": "[yellow]— DISCARDED[/yellow]",
        "needs_review": "[red]⚠ NEEDS REVIEW[/red]",
    }.get(result.action.value, result.action.value)

    rprint(f"\n{icon}")
    if result.target_node:
        rprint(f"  Node: [bold]{result.target_node.title}[/bold]  [{result.target_node.kind}]")
        rprint(f"  ID:   {result.target_node.id}")
    rprint(f"  Placement: depth={result.placement.intended_depth}  "
           f"parent={result.placement.parent_title or 'none'}")
    if result.duplicate_match:
        rprint(f"  Duplicate score: {result.duplicate_match.similarity_score:.3f}")
    if result.needs_human_review:
        rprint(f"\n  [red]Review required:[/red] {result.review_reason}")
    for note in result.consolidation_notes:
        rprint(f"  [dim]{note}[/dim]")


# ---------------------------------------------------------------------------
# Phase 3 — memory stale
# ---------------------------------------------------------------------------


@app.command()
def stale(
    node_id: str = typer.Argument(..., help="Memory node ID to mark as stale"),
    reason: str = typer.Option(..., "--reason", "-r", help="Why is this node stale?"),
) -> None:
    """Mark a memory node as stale.

    The node remains in the tree and is traceable, but will be excluded from
    primary recall results.  Use this when a node's content is outdated but
    its history should be preserved.
    """
    with _get_session() as session:
        svc = PromotionService(session)
        try:
            node = svc.mark_stale(node_id, reason=reason)
        except MemoryNodeNotFoundError:
            rprint(f"[red]Node not found:[/red] {node_id}")
            raise typer.Exit(1)
    rprint(
        f"[yellow]Marked stale:[/yellow] [bold]{node.title}[/bold]  "
        f"(status={node.status.value})"
    )
    rprint(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# Phase 3 — memory relations
# ---------------------------------------------------------------------------


@app.command()
def relations(
    node_id: str = typer.Argument(..., help="Memory node ID"),
) -> None:
    """Display all MemoryRelations for a node."""
    from memory_engine.repositories.relation import RelationRepository
    from memory_engine.models.domain import MemoryRelation

    with _get_session() as session:
        repo = RelationRepository(session)
        rels = [MemoryRelation.model_validate(o) for o in repo.list_by_node(node_id)]

    if not rels:
        rprint("[dim]No relations found for this node.[/dim]")
        return

    table = Table(title=f"Relations for {node_id[:8]}…", show_header=True)
    table.add_column("Source", style="dim")
    table.add_column("Type", style="bold cyan")
    table.add_column("Target", style="dim")
    table.add_column("Created", style="dim")
    for r in rels:
        table.add_row(
            str(r.source_id)[:8] + "…",
            r.relation_type.value,
            str(r.target_id)[:8] + "…",
            r.created_at.strftime("%Y-%m-%d"),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Phase 3 — memory inspect (human-facing, always with evidence)
# ---------------------------------------------------------------------------


@app.command()
def inspect(
    project_name: str = typer.Argument(..., help="Project name"),
    node_id: str = typer.Argument(..., help="Memory node UUID"),
    depth: int = typer.Option(2, "--depth", "-d", help="Child inspection depth"),
) -> None:
    """Inspect a memory node: children, relations, evidence, confidence."""
    project = _find_project(project_name)
    from memory_engine.models.domain import InspectRequest

    req = InspectRequest(
        project_id=project.id,
        memory_id=node_id,
        inspection_depth=depth,
        include_evidence=True,
    )
    with _get_session() as session:
        svc = InspectService(session)
        result = svc.inspect(req)

    m = result.memory
    rprint(f"\n[bold][{m.kind}] {m.title}[/bold]")
    rprint(f"  {m.summary}")
    rprint(
        f"  status=[bold]{m.status.value}[/bold]  "
        f"confidence={m.confidence:.2f}  importance={m.importance:.2f}"
    )
    if m.module_path:
        rprint(f"  module_path={m.module_path}")

    if result.children:
        rprint(f"\n[bold]Children ({len(result.children)}):[/bold]")
        for c in result.children:
            status_tag = (
                f" [yellow]({c.status.value})[/yellow]"
                if c.status.value != "active"
                else ""
            )
            rprint(f"  [{c.kind}] {c.title}{status_tag}")

    if result.related_memories:
        rprint(f"\n[bold]Related memories ({len(result.related_memories)}):[/bold]")
        for r in result.related_memories:
            rprint(f"  [{r.kind}] {r.title}")

    if result.conflicts:
        rprint(f"\n[red]Conflicts ({len(result.conflicts)}):[/red]")
        for c in result.conflicts:
            rprint(f"  [red][{c.kind}] {c.title} (status={c.status.value})[/red]")

    if result.evidence_refs:
        rprint(f"\n[bold]Evidence ({len(result.evidence_refs)}):[/bold]")
        for e in result.evidence_refs:
            src = f"  [{e.source}]" if e.source else ""
            rprint(f"  - {e.content[:120]}{src}")

    ca = result.confidence_assessment
    rprint(
        f"\n[dim]Confidence: {ca.confidence:.2f}  "
        f"Freshness: {ca.freshness:.2f}  Status: {ca.status.value}[/dim]"
    )


# ---------------------------------------------------------------------------
# DEBUG commands
# ---------------------------------------------------------------------------


@debug_app.command("route")
def debug_route(
    project_id: str = typer.Argument(...),
    task: str = typer.Option(..., "--task", "-t"),
    files: Optional[str] = typer.Option(None, "--files"),
    symbols: Optional[str] = typer.Option(None, "--symbols"),
    risk_hint: Optional[RiskLevel] = typer.Option(None, "--risk"),
) -> None:
    """[DEBUG] Run SkillRouter and print the routing plan."""
    rprint("[dim]⚠ Debug command[/dim]")
    req = RouteRequest(
        project_id=UUID(project_id), current_task=task,
        current_files=[f.strip() for f in files.split(",")] if files else [],
        current_symbols=[s.strip() for s in symbols.split(",")] if symbols else [],
        risk_hint=risk_hint,
    )
    plan = SkillRouter().route(req)
    console.print(Syntax(json.dumps(plan.model_dump(), indent=2), "json", theme="monokai"))


@debug_app.command("recall")
def debug_recall(
    project_id: str = typer.Argument(...),
    task: str = typer.Option(..., "--task", "-t"),
    files: Optional[str] = typer.Option(None, "--files"),
    symbols: Optional[str] = typer.Option(None, "--symbols"),
    budget: Optional[int] = typer.Option(None, "--budget"),
) -> None:
    """[DEBUG] Run recall_memory and display ContextPack + trace."""
    rprint("[dim]⚠ Debug command[/dim]")
    from memory_engine.models.domain import RecallRequest
    req = RecallRequest(
        project_id=UUID(project_id), current_task=task,
        current_files=[f.strip() for f in files.split(",")] if files else [],
        current_symbols=[s.strip() for s in symbols.split(",")] if symbols else [],
        token_budget=budget,
    )
    with _get_session() as session:
        result = RecallService(session).recall(req)
    if result.recall_skipped:
        rprint(f"[yellow]Skipped:[/yellow] {result.skip_reason}")
        return
    rprint(result.context_pack.as_text())
    rprint(f"\n[bold]Token estimate:[/bold] {result.token_estimate}")
    rprint("\n[bold]Trace:[/bold]")
    for e in result.retrieval_trace:
        icon = "[green]✓[/green]" if e.action == "selected" else "[red]✗[/red]"
        rprint(f"  {icon} {e.title!r:40s} score={e.score:.3f}  {e.reason}")


@debug_app.command("inspect")
def debug_inspect(
    project_id: str = typer.Argument(...),
    memory_id: str = typer.Argument(...),
    depth: int = typer.Option(1, "--depth"),
    no_evidence: bool = typer.Option(False, "--no-evidence"),
) -> None:
    """[DEBUG] Inspect a memory node."""
    rprint("[dim]⚠ Debug command[/dim]")
    from memory_engine.models.domain import InspectRequest
    req = InspectRequest(
        project_id=UUID(project_id), memory_id=memory_id,
        inspection_depth=depth, include_evidence=not no_evidence,
    )
    with _get_session() as session:
        result = InspectService(session).inspect(req)
    m = result.memory
    rprint(f"\n[bold][{m.kind}] {m.title}[/bold]  status={m.status.value}")
    rprint(f"  {m.summary}")
    if result.children:
        rprint(f"\nChildren: {', '.join(c.title for c in result.children)}")
    if result.conflicts:
        rprint(f"\n[red]Conflicts: {', '.join(c.title for c in result.conflicts)}[/red]")
    ca = result.confidence_assessment
    rprint(f"\n[dim]confidence={ca.confidence:.2f} freshness={ca.freshness:.2f}[/dim]")


@debug_app.command("tree")
def debug_tree(project_name: str = typer.Argument(...)) -> None:
    """[DEBUG] Print the full memory tree."""
    rprint("[dim]⚠ Debug command[/dim]")
    project = _find_project(project_name)
    with _get_session() as session:
        text = TreeService(session).render_text(str(project.id))
    rprint(f"[bold]{project_name}[/bold]")
    console.print(text or "(empty)")


# ---------------------------------------------------------------------------
# Phase 11 — memory retention
# ---------------------------------------------------------------------------

retention_app = typer.Typer(help="Memory retention lifecycle commands", no_args_is_help=True)
app.add_typer(retention_app, name="retention")


def _get_retention_svc(project_name: str):  # type: ignore[no-untyped-def]
    from memory_engine.services.retention import MemoryRetentionService
    project = _find_project(project_name)
    session = _get_session()
    svc = MemoryRetentionService(session, str(project.id))
    return svc, session


@retention_app.command("status")
def retention_status(
    project_name: str = typer.Argument(..., help="Project name"),
) -> None:
    """Show retention diagnostics for a project."""
    from memory_engine.services.retention import MemoryRetentionService
    project = _find_project(project_name)
    with _get_session() as session:
        svc = MemoryRetentionService(session, str(project.id))
        report = svc.generate_report()

    table = Table(title=f"Retention Status — {project_name}", show_header=True)
    table.add_column("State", style="bold")
    table.add_column("Count", justify="right")
    counts = report.to_dict()["counts"]
    for state, count in counts.items():
        style = "green" if state == "active" else ("yellow" if state in ("stale", "needs_review") else "dim")
        table.add_row(state.replace("_", " ").title(), f"[{style}]{count}[/{style}]")
    console.print(table)
    if report.warnings:
        for w in report.warnings:
            rprint(f"[yellow]⚠[/yellow] {w}")


@retention_app.command("report")
def retention_report(
    project_name: str = typer.Argument(..., help="Project name"),
) -> None:
    """Print full retention report (archive/expiry/compaction candidates)."""
    from memory_engine.services.retention import MemoryRetentionService
    project = _find_project(project_name)
    with _get_session() as session:
        svc = MemoryRetentionService(session, str(project.id))
        report = svc.generate_report()

    console.print(Syntax(json.dumps(report.to_dict(), indent=2), "json", theme="monokai"))


@retention_app.command("run")
def retention_run(
    project_name: str = typer.Argument(..., help="Project name"),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Simulate only"),
) -> None:
    """Run retention lifecycle (expiry, archival, compaction)."""
    from memory_engine.services.retention import MemoryRetentionService
    project = _find_project(project_name)
    with _get_session() as session:
        svc = MemoryRetentionService(session, str(project.id))
        report = svc.run(dry_run=dry_run)
        if not dry_run:
            session.commit()

    mode = "[yellow]DRY RUN[/yellow]" if dry_run else "[green]APPLIED[/green]"
    rprint(f"\n{mode} — Retention cycle complete")
    rprint(f"  Archive actions: {len(report.archive_actions)}")
    rprint(f"  Expiry actions:  {len(report.expiry_actions)}")
    rprint(f"  Compaction groups found: {report.compaction_groups_found}")
    rprint(f"  Compactions applied: {len(report.compaction_results)}")
    if dry_run:
        rprint("\n[dim]Pass --no-dry-run to apply changes.[/dim]")


@retention_app.command("restore")
def retention_restore(
    project_name: str = typer.Argument(..., help="Project name"),
    memory_id: str = typer.Argument(..., help="Memory node UUID to restore"),
) -> None:
    """Restore an archived memory node to active status."""
    from memory_engine.services.retention import MemoryRetentionService
    project = _find_project(project_name)
    with _get_session() as session:
        svc = MemoryRetentionService(session, str(project.id))
        node = svc.restore_memory(memory_id)
        if node is None:
            rprint(f"[red]Memory not found:[/red] {memory_id}")
            raise typer.Exit(1)
        session.commit()
    rprint(f"[green]✓[/green] Restored: [bold]{node.title}[/bold] → status=active")


# ---------------------------------------------------------------------------
# Phase 11 — memory policy
# ---------------------------------------------------------------------------

policy_app = typer.Typer(help="Agent memory policy generation and installation", no_args_is_help=True)
app.add_typer(policy_app, name="policy")


def _resolve_root(project_root: Optional[str]) -> "Path":
    from pathlib import Path
    if project_root:
        return Path(project_root).resolve()
    # Try git-based resolution first
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, shell=False, check=True,
        )
        return Path(result.stdout.strip())
    except Exception:
        return Path(".").resolve()


@policy_app.command("generate")
def policy_generate(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
    mcp_server: str = typer.Option("memory-engine", "--mcp-server"),
) -> None:
    """Generate the canonical AGENT_MEMORY_POLICY.md."""
    from memory_engine.policy.generator import generate_policy
    root = _resolve_root(project_root)
    path = generate_policy(root, mcp_server)
    rprint(f"[green]✓[/green] Policy written: [bold]{path}[/bold]")


@policy_app.command("install")
def policy_install(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
    client: str = typer.Option(..., "--client", "-c", help="claude-code | cursor"),
    mcp_server: str = typer.Option("memory-engine", "--mcp-server"),
) -> None:
    """Install client-specific policy adapter (claude-code or cursor)."""
    from memory_engine.policy.installer import install_claude_code, install_cursor
    root = _resolve_root(project_root)
    if client == "claude-code":
        path = install_claude_code(root, mcp_server)
    elif client == "cursor":
        path = install_cursor(root, mcp_server)
    else:
        rprint(f"[red]Unknown client:[/red] {client}. Use 'claude-code' or 'cursor'.")
        raise typer.Exit(1)
    rprint(f"[green]✓[/green] Installed [{client}] adapter: [bold]{path}[/bold]")


@policy_app.command("status")
def policy_status_cmd(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
) -> None:
    """Show policy generation and adapter installation status."""
    from memory_engine.policy.installer import adapter_status
    root = _resolve_root(project_root)
    status = adapter_status(root)
    console.print(Syntax(json.dumps(status, indent=2), "json", theme="monokai"))


@policy_app.command("remove")
def policy_remove(
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p"),
    client: str = typer.Option(..., "--client", "-c", help="claude-code | cursor"),
) -> None:
    """Remove a generated policy block from a client adapter file."""
    from memory_engine.policy.installer import remove_adapter
    root = _resolve_root(project_root)
    result = remove_adapter(root, client)
    if result:
        rprint(f"[green]✓[/green] Removed [{client}] policy block from {result}")
    else:
        rprint(f"[dim]No [{client}] policy block found to remove.[/dim]")


if __name__ == "__main__":
    app()
