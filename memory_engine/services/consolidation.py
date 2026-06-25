"""ConsolidationService — updates parent summaries after child node changes.

Rules:
  - Summaries are built from child node titles and one-line snippets.
  - Raw evidence content is never inserted into a parent summary.
  - Stale / superseded / archived children are excluded from the summary.
  - Operation is idempotent — re-running produces the same output.
  - The generated suffix is always appended after the original base text.

Generated suffix format:
  " Includes: '<title1>', '<title2>', '<title3>'."

If a parent has no active children the suffix is omitted and the original
summary base is preserved unchanged.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory_engine.repositories.memory_node import MemoryNodeRepository

# Sentinel used to split original summary from generated suffix
_SUFFIX_SENTINEL = " Includes:"


class ConsolidationService:
    def __init__(self, session: Session) -> None:
        self._nodes = MemoryNodeRepository(session)

    def update_parent(self, parent_id: str) -> list[str]:
        """Rebuild the parent summary to reflect current active children.

        Returns a list of human-readable notes describing what changed.
        """
        parent = self._nodes.get_bare(parent_id)
        if parent is None:
            return [f"Parent node {parent_id!r} not found — skipped."]

        children = self._nodes.list_children(parent_id)
        active_children = [
            c for c in children
            if c.status in ("active",)
        ]

        # Extract the original base (strip any previously generated suffix)
        base = parent.summary.split(_SUFFIX_SENTINEL)[0].rstrip()

        if not active_children:
            # Nothing to add — reset to base
            new_summary = base
        else:
            titles = ", ".join(f"'{c.title}'" for c in active_children)
            new_summary = f"{base}{_SUFFIX_SENTINEL} {titles}."

        notes: list[str] = []
        if new_summary != parent.summary:
            self._nodes.update_summary(parent_id, new_summary)
            notes.append(
                f"Consolidated parent '{parent.title}': "
                f"{len(active_children)} active children listed in summary."
            )
        else:
            notes.append(
                f"Parent '{parent.title}' summary already up-to-date."
            )

        return notes

    def update_ancestors(self, node_id: str) -> list[str]:
        """Walk up the tree from node_id and consolidate each ancestor.

        Used after a deep insertion to propagate changes to all affected parents.
        """
        all_notes: list[str] = []
        current_id = node_id

        # Safety cap: never walk more than max_tree_depth ancestors
        for _ in range(10):
            node = self._nodes.get_bare(current_id)
            if node is None or node.parent_id is None:
                break
            all_notes.extend(self.update_parent(node.parent_id))
            current_id = node.parent_id

        return all_notes
