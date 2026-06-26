"""Paragraph segmenter — Phase 10.

Produces KnowledgeParagraphORM-compatible records from raw source content.
Reuses chunker boundaries but adds richer metadata:
  - summary (first sentence of docstring, or first meaningful line)
  - symbol_names (extracted def/class names)
  - section_heading (markdown heading or class/function name)
  - heading_path (nested heading stack)
  - source span (start_line, end_line)

Paragraphs map to:
  Code     → each function/class/method block
  Markdown → each heading section (with content)
  Other    → fall back to chunker output as paragraphs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory_engine.knowledge.chunkers import (
    RawChunk,
    _extract_symbols,
    _infer_module_path,
    _HEADING_RE,
    _split_paragraphs,
    _TOP_LEVEL_DEF_RE,
    MAX_SECTION_TOKENS,
    MAX_CODE_CHUNK_TOKENS,
    _tokens,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RawParagraph:
    content: str
    summary: str | None
    symbol_names: list[str] = field(default_factory=list)
    section_heading: str | None = None
    heading_path: list[str] = field(default_factory=list)
    paragraph_index: int = 0
    source_path: str | None = None
    source_start_line: int | None = None
    source_end_line: int | None = None

    @property
    def token_count(self) -> int:
        return max(1, len(self.content) // 4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOCSTRING_FIRST_LINE_RE = re.compile(
    r'(?:"""|\'\'\')(.*?)(?:"""|\'\'\')|("""|\'\'\')(.*)',
    re.DOTALL,
)
_SINGLE_DOCSTRING_RE = re.compile(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', re.DOTALL)


def _extract_summary(content: str) -> str | None:
    """Extract first meaningful line from a block (docstring or first sentence)."""
    # Try docstring first
    m = _SINGLE_DOCSTRING_RE.search(content)
    if m:
        raw = (m.group(1) or m.group(2) or "").strip()
        if raw:
            first_line = raw.splitlines()[0].strip()
            if len(first_line) > 10:
                return first_line[:200]

    # Fall back to first non-empty, non-decorator, non-import line
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("@", "#!", "import ", "from ", "#")):
            continue
        if re.match(r"^(?:def |async def |class )", stripped):
            # Extract just the signature as summary
            sig = stripped.rstrip(":")
            if len(sig) > 5:
                return sig[:200]
            continue
        if len(stripped) > 10:
            return stripped[:200]
    return None


# ---------------------------------------------------------------------------
# Code paragraph segmentation
# ---------------------------------------------------------------------------


def segment_code(
    text: str,
    source_path: str | None = None,
    language: str = "python",
) -> list[RawParagraph]:
    """Segment code into per-function/class paragraphs."""
    module_paths = _infer_module_path(source_path)
    lines = text.splitlines(keepends=True)
    paragraphs: list[RawParagraph] = []

    split_lines: list[int] = [0]
    for i, line in enumerate(lines):
        if _TOP_LEVEL_DEF_RE.match(line):
            split_lines.append(i)
    split_lines = sorted(set(split_lines))

    for j, start in enumerate(split_lines):
        end = split_lines[j + 1] if j + 1 < len(split_lines) else len(lines)
        segment = "".join(lines[start:end]).strip()
        if not segment:
            continue

        # Further split oversized blocks at paragraph boundaries
        sub_segments: list[tuple[str, int, int]] = []
        if _tokens(segment) > MAX_CODE_CHUNK_TOKENS:
            parts = re.split(r"\n{2,}", segment)
            sub_start = start
            for part in parts:
                if part.strip():
                    part_lines = part.count("\n") + 1
                    sub_segments.append((part.strip(), sub_start + 1, sub_start + part_lines))
                    sub_start += part_lines + 1
        else:
            sub_segments = [(segment, start + 1, end)]

        for content, sl, el in sub_segments:
            if not content.strip():
                continue
            symbols = _extract_symbols(content)
            # Section heading = first def/class name
            heading = None
            first_def = re.match(r"(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", content)
            if first_def:
                heading = first_def.group(1)

            paragraphs.append(RawParagraph(
                content=content,
                summary=_extract_summary(content),
                symbol_names=symbols,
                section_heading=heading,
                heading_path=module_paths + ([heading] if heading else []),
                paragraph_index=len(paragraphs),
                source_path=source_path,
                source_start_line=sl,
                source_end_line=el,
            ))

    return paragraphs or [RawParagraph(
        content=text.strip(),
        summary=_extract_summary(text),
        symbol_names=_extract_symbols(text),
        paragraph_index=0,
        source_path=source_path,
        source_start_line=1,
        source_end_line=len(lines),
    )]


# ---------------------------------------------------------------------------
# Markdown paragraph segmentation
# ---------------------------------------------------------------------------


def segment_markdown(
    text: str,
    source_path: str | None = None,
) -> list[RawParagraph]:
    """Segment markdown into per-heading-section paragraphs."""
    paragraphs: list[RawParagraph] = []
    heading_stack: list[str] = []
    last_heading_stack: list[str] = []
    sections: list[tuple[list[str], str, int, int]] = []  # (hpath, content, start_line, end_line)

    matches = list(_HEADING_RE.finditer(text))
    lines = text.splitlines()

    def _line_of(pos: int) -> int:
        return text[:pos].count("\n") + 1

    def _flush(start_pos: int, end_pos: int, hstack: list[str]) -> None:
        section = text[start_pos:end_pos].strip()
        if section:
            sections.append((
                list(hstack),
                section,
                _line_of(start_pos),
                _line_of(end_pos),
            ))

    for i, m in enumerate(matches):
        if i > 0:
            _flush(matches[i - 1].start(), m.start(), last_heading_stack)
        level = len(m.group(1))
        heading = m.group(2).strip()
        heading_stack = heading_stack[:level - 1] + [heading]
        last_heading_stack = list(heading_stack)

    if matches:
        _flush(matches[-1].start(), len(text), last_heading_stack)
    else:
        sections.append(([], text.strip(), 1, len(lines)))

    idx = 0
    for hpath, section, sl, el in sections:
        for sub in _split_paragraphs(section, MAX_SECTION_TOKENS):
            if not sub.strip():
                continue
            heading = hpath[-1] if hpath else None
            paragraphs.append(RawParagraph(
                content=sub,
                summary=_extract_summary(sub),
                symbol_names=[],
                section_heading=heading,
                heading_path=hpath,
                paragraph_index=idx,
                source_path=source_path,
                source_start_line=sl,
                source_end_line=el,
            ))
            idx += 1

    return paragraphs


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_CODE_SOURCE_TYPES = frozenset({"code_file", "code_comment"})
_MARKDOWN_SOURCE_TYPES = frozenset({
    "markdown", "readme", "architecture_doc", "adr", "api_spec",
    "manual_note", "task_artifact",
})


def segment_paragraphs(
    text: str,
    source_type: str,
    source_path: str | None = None,
    language: str | None = None,
) -> list[RawParagraph]:
    """Route to the correct paragraph segmenter based on source type."""
    if source_type in _CODE_SOURCE_TYPES:
        lang = language or "python"
        return segment_code(text, source_path=source_path, language=lang)
    if source_type in _MARKDOWN_SOURCE_TYPES:
        return segment_markdown(text, source_path=source_path)
    # test_report, runtime_log, git_diff — treat each chunk as one paragraph
    from memory_engine.knowledge.chunkers import chunk_content
    raw_chunks = chunk_content(text, source_type, source_path=source_path)
    return [
        RawParagraph(
            content=rc.content,
            summary=_extract_summary(rc.content),
            symbol_names=rc.related_symbols,
            section_heading=rc.heading_path[-1] if rc.heading_path else None,
            heading_path=rc.heading_path,
            paragraph_index=i,
            source_path=source_path,
            source_start_line=rc.start_line,
            source_end_line=rc.end_line,
        )
        for i, rc in enumerate(raw_chunks)
    ]
