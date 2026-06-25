"""Tree rendering service — builds hierarchical views of memory nodes."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from memory_engine.models.domain import MemoryNode
from memory_engine.repositories.memory_node import MemoryNodeRepository


@dataclass
class TreeNode:
    """A node in the rendered tree — wraps a MemoryNode with its children."""

    node: MemoryNode
    children: list["TreeNode"] = field(default_factory=list)


class TreeService:
    def __init__(self, session: Session) -> None:
        self._repo = MemoryNodeRepository(session)

    def build_tree(self, project_id: str) -> list[TreeNode]:
        """Return a list of root TreeNodes with nested children for a project."""
        all_nodes = [
            MemoryNode.model_validate(o)
            for o in self._repo.list_by_project(project_id)
        ]

        # Index all nodes by id
        index: dict[str, TreeNode] = {str(n.id): TreeNode(node=n) for n in all_nodes}

        roots: list[TreeNode] = []
        for tree_node in index.values():
            parent_id = tree_node.node.parent_id
            if parent_id is None:
                roots.append(tree_node)
            else:
                parent = index.get(str(parent_id))
                if parent is not None:
                    parent.children.append(tree_node)

        return roots

    def render_text(self, project_id: str) -> str:
        """Return an ASCII tree string suitable for CLI display."""
        roots = self.build_tree(project_id)
        lines: list[str] = []
        self._render_nodes(roots, lines, prefix="")
        return "\n".join(lines)

    def _render_nodes(
        self, nodes: list[TreeNode], lines: list[str], prefix: str
    ) -> None:
        for i, tree_node in enumerate(nodes):
            is_last = i == len(nodes) - 1
            connector = "└── " if is_last else "├── "
            n = tree_node.node
            lines.append(
                f"{prefix}{connector}[{n.kind}] {n.title}"
                + (f"  ({', '.join(n.tags)})" if n.tags else "")
            )
            child_prefix = prefix + ("    " if is_last else "│   ")
            self._render_nodes(tree_node.children, lines, child_prefix)
