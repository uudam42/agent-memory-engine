"""Deterministic chunk/module/document summarizer — Phase 10.

Produces KnowledgeChunkSummaryORM-compatible records from lists of paragraphs
and chunks.  No LLM required.  Summaries are built from:
  - Symbol names extracted from code
  - Heading paths from markdown sections
  - First sentences of docstrings
  - Constraint/security patterns detected in content

Granularity levels:
  chunk    — summary of a few paragraphs (~1 function or section)
  module   — summary of a complete file
  document — summary at document/repo level (not built here automatically)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory_engine.knowledge.paragraph_segmenter import RawParagraph


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RawSummary:
    summary_text: str
    purpose: str | None
    key_symbols: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    constraints_mentioned: list[str] = field(default_factory=list)
    important_interactions: list[str] = field(default_factory=list)
    granularity_level: str = "chunk"  # chunk | module | document
    source_start_line: int | None = None
    source_end_line: int | None = None
    token_count: int = 0

    @property
    def content_hash_input(self) -> str:
        return self.summary_text + "|" + "|".join(self.key_symbols)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONSTRAINT_RE = re.compile(
    r"(?:must(?:\s+not)?|cannot|can't|never|always|required|"
    r"forbidden|prohibited|shell\s*=\s*False|allowlist|blocklist)\b[^.!?]*[.!?]?",
    re.I,
)
_IMPORT_RE = re.compile(r"^(?:import|from)\s+(\w[\w.]*)", re.MULTILINE)
_INTERACTION_CLASSES_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:Service|Manager|Resolver|Index|Engine|Store|Cache|Repository|Coordinator|Adapter|Handler))\b")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _extract_constraints(text: str) -> list[str]:
    constraints: list[str] = []
    for m in _CONSTRAINT_RE.finditer(text):
        c = m.group(0).strip()
        if 10 < len(c) < 200:
            constraints.append(c)
    return _dedupe(constraints)[:5]


def _extract_interactions(text: str) -> list[str]:
    return _dedupe(_INTERACTION_CLASSES_RE.findall(text))[:8]


def _extract_imported_modules(text: str) -> list[str]:
    return _dedupe(_IMPORT_RE.findall(text))[:8]


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Chunk-level summary (a few paragraphs)
# ---------------------------------------------------------------------------


def summarize_paragraphs(
    paragraphs: list[RawParagraph],
    granularity_level: str = "chunk",
) -> RawSummary | None:
    """Produce a single summary from a list of paragraphs (one code file section or doc section)."""
    if not paragraphs:
        return None

    all_symbols: list[str] = []
    all_headings: list[str] = []
    all_content: list[str] = []
    summaries: list[str] = []
    responsibilities: list[str] = []

    for para in paragraphs:
        all_symbols.extend(para.symbol_names)
        if para.section_heading:
            all_headings.append(para.section_heading)
        all_content.append(para.content)
        if para.summary:
            summaries.append(para.summary)

    combined = "\n\n".join(all_content)
    key_symbols = _dedupe(all_symbols)[:10]
    constraints = _extract_constraints(combined)
    interactions = _extract_interactions(combined)

    # Build responsibilities from para summaries
    for s in summaries[:6]:
        if len(s) > 12 and s not in responsibilities:
            responsibilities.append(s)

    # Build summary_text
    if all_headings:
        scope = ", ".join(_dedupe(all_headings)[:3])
        if summaries:
            purpose = summaries[0]
            summary_text = f"{scope}: {purpose}"
        else:
            summary_text = f"Covers: {scope}."
    elif key_symbols:
        scope = ", ".join(key_symbols[:4])
        summary_text = f"Defines {scope}."
        if summaries:
            summary_text += f" {summaries[0]}"
    elif summaries:
        summary_text = summaries[0]
    else:
        # Last resort: first non-empty line
        first_line = next((l.strip() for l in combined.splitlines() if l.strip()), "")
        summary_text = first_line[:200] if first_line else "No summary available."

    purpose = summaries[0] if summaries else None

    start_line = min(
        (p.source_start_line for p in paragraphs if p.source_start_line is not None),
        default=None,
    )
    end_line = max(
        (p.source_end_line for p in paragraphs if p.source_end_line is not None),
        default=None,
    )

    return RawSummary(
        summary_text=summary_text[:600],
        purpose=purpose,
        key_symbols=key_symbols,
        responsibilities=responsibilities[:6],
        constraints_mentioned=constraints,
        important_interactions=interactions,
        granularity_level=granularity_level,
        source_start_line=start_line,
        source_end_line=end_line,
        token_count=_tokens(summary_text),
    )


# ---------------------------------------------------------------------------
# Module-level summary (all paragraphs in a file)
# ---------------------------------------------------------------------------


def summarize_module(
    paragraphs: list[RawParagraph],
    source_path: str | None = None,
) -> RawSummary | None:
    """Produce a module-level summary from all paragraphs in a file."""
    if not paragraphs:
        return None

    all_symbols: list[str] = []
    all_headings: list[str] = []
    all_content: list[str] = []
    summaries: list[str] = []

    for para in paragraphs:
        all_symbols.extend(para.symbol_names)
        if para.section_heading:
            all_headings.append(para.section_heading)
        all_content.append(para.content)
        if para.summary and para.paragraph_index == 0:
            summaries.append(para.summary)  # first para summary = module doc

    combined = "\n\n".join(all_content)
    key_symbols = _dedupe(all_symbols)[:15]
    constraints = _extract_constraints(combined)
    interactions = _extract_interactions(combined)
    imported = _extract_imported_modules(combined)

    # Module name from path
    module_name = ""
    if source_path:
        module_name = source_path.replace("\\", "/").rsplit("/", 1)[-1]
        module_name = re.sub(r"\.py$|\.md$|\.ts$", "", module_name)

    # Build summary_text
    if summaries:
        purpose = summaries[0]
    elif all_headings:
        purpose = f"Module covering: {', '.join(_dedupe(all_headings)[:4])}"
    elif key_symbols:
        purpose = f"Defines: {', '.join(key_symbols[:5])}"
    else:
        purpose = f"Source file: {module_name}" if module_name else "Module summary."

    if key_symbols:
        summary_text = f"{purpose} Key components: {', '.join(key_symbols[:6])}."
    else:
        summary_text = purpose

    responsibilities = [s for s in summaries[1:7] if len(s) > 12]

    return RawSummary(
        summary_text=summary_text[:800],
        purpose=purpose,
        key_symbols=key_symbols,
        responsibilities=responsibilities,
        constraints_mentioned=constraints,
        important_interactions=interactions + imported,
        granularity_level="module",
        source_start_line=min(
            (p.source_start_line for p in paragraphs if p.source_start_line is not None),
            default=1,
        ),
        source_end_line=max(
            (p.source_end_line for p in paragraphs if p.source_end_line is not None),
            default=None,
        ),
        token_count=_tokens(summary_text),
    )
