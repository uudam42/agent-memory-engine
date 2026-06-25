"""Content chunkers for each source type.

Each chunker accepts raw (already-redacted) text and returns a list of
RawChunk dataclasses.  The caller (KnowledgeIngestionService) handles
persistence and hashing.

Chunking rules:
  markdown / readme / architecture_doc / adr / api_spec / manual_note / task_artifact:
    Split on # headings; oversized sections (> MAX_SECTION_TOKENS) are
    further paragraph-split.

  code_file / code_comment:
    Split on top-level def/class boundaries.  Each function/class body
    becomes one chunk.  An initial "module header" chunk captures imports.

  test_report:
    Split on PASSED / FAILED / ERROR test-result sections.

  runtime_log:
    Group consecutive error/warning lines with 3 lines of context into
    event windows; blank-line-separate other lines into paragraphs.

  git_diff:
    Split on `diff --git` file boundaries, then on `@@` hunk boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ~4 chars per token (deterministic approximation used everywhere)
_CHARS_PER_TOKEN = 4
MAX_SECTION_TOKENS = 1200
MAX_CODE_CHUNK_TOKENS = 1000
MAX_LOG_WINDOW_TOKENS = 600


def _tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass
class RawChunk:
    content: str
    chunk_index: int
    heading_path: list[str] = field(default_factory=list)
    module_paths: list[str] = field(default_factory=list)
    related_symbols: list[str] = field(default_factory=list)
    language: str | None = None
    source_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    @property
    def token_count(self) -> int:
        return _tokens(self.content)


# ---------------------------------------------------------------------------
# Markdown / ADR / documentation chunker
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)", re.MULTILINE)


def _split_paragraphs(text: str, max_tokens: int) -> list[str]:
    """Further split text on blank lines when it exceeds max_tokens."""
    if _tokens(text) <= max_tokens:
        return [text]
    paras = re.split(r"\n\n+", text)
    chunks: list[str] = []
    current = ""
    for para in paras:
        candidate = (current + "\n\n" + para).strip() if current else para
        if _tokens(candidate) > max_tokens and current:
            chunks.append(current.strip())
            current = para
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


def chunk_markdown(text: str, source_path: str | None = None) -> list[RawChunk]:
    """Split on headings, then paragraph-split oversized sections."""
    chunks: list[RawChunk] = []
    heading_stack: list[str] = []
    last_pos = 0
    last_heading_stack: list[str] = []
    sections: list[tuple[list[str], str]] = []

    matches = list(_HEADING_RE.finditer(text))

    def _flush(start: int, end: int, hstack: list[str]) -> None:
        section = text[start:end].strip()
        if section:
            sections.append((list(hstack), section))

    for i, m in enumerate(matches):
        if i > 0:
            _flush(matches[i - 1].start(), m.start(), last_heading_stack)
        level = len(m.group(1))
        heading = m.group(2).strip()
        heading_stack = heading_stack[:level - 1] + [heading]
        last_heading_stack = list(heading_stack)

    # Flush final section
    if matches:
        _flush(matches[-1].start(), len(text), last_heading_stack)
    else:
        # No headings at all — treat as one section
        sections.append(([], text.strip()))

    idx = 0
    for hpath, section in sections:
        for sub in _split_paragraphs(section, MAX_SECTION_TOKENS):
            if sub.strip():
                chunks.append(RawChunk(
                    content=sub,
                    chunk_index=idx,
                    heading_path=hpath,
                ))
                idx += 1
    return chunks


# ---------------------------------------------------------------------------
# Code file chunker
# ---------------------------------------------------------------------------

_TOP_LEVEL_DEF_RE = re.compile(r"^(def |class |async def )", re.MULTILINE)
_SYMBOL_RE = re.compile(r"^(?:def |async def |class )\s*([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)


def _extract_symbols(code: str) -> list[str]:
    return _SYMBOL_RE.findall(code)


def _infer_module_path(source_path: str | None) -> list[str]:
    if not source_path:
        return []
    path = source_path.replace("\\", "/").rstrip("/")
    # Remove leading src/ app/ etc.
    path = re.sub(r"^(?:src|app|lib)/", "", path)
    # Remove .py extension
    path = re.sub(r"\.py$", "", path)
    dotted = path.replace("/", ".")
    return [dotted] if dotted else []


def chunk_code(
    text: str,
    source_path: str | None = None,
    language: str = "python",
) -> list[RawChunk]:
    """Split on top-level def/class boundaries."""
    module_paths = _infer_module_path(source_path)
    lines = text.splitlines(keepends=True)
    chunks: list[RawChunk] = []

    # Find positions of top-level definitions
    split_lines: list[int] = [0]  # module header starts at line 0
    for i, line in enumerate(lines):
        if _TOP_LEVEL_DEF_RE.match(line):
            split_lines.append(i)

    # Remove duplicate starts
    split_lines = sorted(set(split_lines))

    for j, start in enumerate(split_lines):
        end = split_lines[j + 1] if j + 1 < len(split_lines) else len(lines)
        segment = "".join(lines[start:end]).strip()
        if not segment:
            continue

        # Further split oversized blocks
        sub_chunks = _split_paragraphs(segment, MAX_CODE_CHUNK_TOKENS)
        for sub in sub_chunks:
            if sub.strip():
                symbols = _extract_symbols(sub)
                chunks.append(RawChunk(
                    content=sub,
                    chunk_index=len(chunks),
                    module_paths=module_paths,
                    related_symbols=symbols,
                    language=language,
                    source_path=source_path,
                    start_line=start + 1,
                    end_line=min(end, len(lines)),
                ))

    return chunks or [RawChunk(
        content=text.strip(),
        chunk_index=0,
        module_paths=module_paths,
        related_symbols=_extract_symbols(text),
        language=language,
        source_path=source_path,
        start_line=1,
        end_line=len(lines),
    )]


# ---------------------------------------------------------------------------
# Test report chunker
# ---------------------------------------------------------------------------

_TEST_SECTION_RE = re.compile(
    r"(^(?:PASSED|FAILED|ERROR|test_[A-Za-z0-9_]+|={5,}|-{5,}|FAILURES|ERRORS).*$)",
    re.MULTILINE,
)


def chunk_test_report(text: str) -> list[RawChunk]:
    """Split on PASSED/FAILED/ERROR test-result section boundaries."""
    lines = text.splitlines()
    chunks: list[RawChunk] = []
    current_lines: list[str] = []
    current_start = 1

    def _flush(end: int) -> None:
        block = "\n".join(current_lines).strip()
        if block:
            symbols = re.findall(r"test_[A-Za-z0-9_]+", block)
            chunks.append(RawChunk(
                content=block,
                chunk_index=len(chunks),
                related_symbols=symbols,
                start_line=current_start,
                end_line=end,
            ))

    for i, line in enumerate(lines, 1):
        if _TEST_SECTION_RE.match(line) and current_lines:
            _flush(i - 1)
            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

    _flush(len(lines))
    return chunks or [RawChunk(content=text.strip(), chunk_index=0)]


# ---------------------------------------------------------------------------
# Log chunker
# ---------------------------------------------------------------------------

_ERROR_LINE_RE = re.compile(r"(?:ERROR|WARN(?:ING)?|CRITICAL|EXCEPTION|Traceback)", re.I)
_CONTEXT_LINES = 3


def chunk_log(text: str) -> list[RawChunk]:
    """Group consecutive error/warning lines with context into event windows."""
    lines = text.splitlines()
    n = len(lines)
    in_window: set[int] = set()

    # Mark error lines and context
    for i, line in enumerate(lines):
        if _ERROR_LINE_RE.search(line):
            for j in range(max(0, i - _CONTEXT_LINES), min(n, i + _CONTEXT_LINES + 1)):
                in_window.add(j)

    # Build contiguous segments
    chunks: list[RawChunk] = []
    current: list[str] = []
    current_start = 0
    in_seg = False

    def _flush(end: int) -> None:
        block = "\n".join(current).strip()
        if block:
            # Further split if too long
            for sub in _split_paragraphs(block, MAX_LOG_WINDOW_TOKENS):
                if sub.strip():
                    chunks.append(RawChunk(
                        content=sub,
                        chunk_index=len(chunks),
                        start_line=current_start + 1,
                        end_line=end,
                    ))

    for i, line in enumerate(lines):
        if i in in_window:
            if not in_seg:
                current_start = i
                in_seg = True
            current.append(line)
        else:
            if in_seg:
                _flush(i)
                current = []
                in_seg = False

    if in_seg:
        _flush(len(lines))

    # Fallback: if nothing was flagged, treat the whole log as one chunk
    return chunks or [RawChunk(content=text.strip(), chunk_index=0)]


# ---------------------------------------------------------------------------
# Git diff chunker
# ---------------------------------------------------------------------------

_DIFF_FILE_RE = re.compile(r"^diff --git ", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ ", re.MULTILINE)


def _file_stem(diff_header: str) -> str | None:
    m = re.search(r"b/(\S+)", diff_header)
    return m.group(1) if m else None


def chunk_diff(text: str) -> list[RawChunk]:
    """Split by file boundary, then by hunk boundary."""
    chunks: list[RawChunk] = []
    file_sections = _DIFF_FILE_RE.split(text)

    for file_sec in file_sections:
        if not file_sec.strip():
            continue
        file_stem = _file_stem("diff --git " + file_sec) or ""
        module_paths = [file_stem.replace("/", ".").rstrip(".py")] if file_stem else []

        # Split each file section into hunks
        hunks = _HUNK_RE.split(file_sec)
        for hunk in hunks:
            if hunk.strip():
                added = re.findall(r"^\+[^+].*", hunk, re.MULTILINE)
                symbols = []
                for line in added:
                    symbols += _extract_symbols(line)

                chunks.append(RawChunk(
                    content=("@@ " + hunk if not hunk.startswith("diff") else hunk).strip(),
                    chunk_index=len(chunks),
                    module_paths=module_paths,
                    related_symbols=list(dict.fromkeys(symbols)),
                ))

    return chunks or [RawChunk(content=text.strip(), chunk_index=0)]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_SOURCE_TYPE_DISPATCH = {
    "markdown": chunk_markdown,
    "readme": chunk_markdown,
    "architecture_doc": chunk_markdown,
    "adr": chunk_markdown,
    "api_spec": chunk_markdown,
    "manual_note": chunk_markdown,
    "task_artifact": chunk_markdown,
    "code_file": chunk_code,
    "code_comment": chunk_code,
    "test_report": chunk_test_report,
    "runtime_log": chunk_log,
    "git_diff": chunk_diff,
}


def chunk_content(
    text: str,
    source_type: str,
    source_path: str | None = None,
    language: str | None = None,
) -> list[RawChunk]:
    """Route to the correct chunker based on source type."""
    fn = _SOURCE_TYPE_DISPATCH.get(source_type, chunk_markdown)
    if source_type in ("code_file", "code_comment"):
        lang = language or _infer_language(source_path)
        return fn(text, source_path=source_path, language=lang)  # type: ignore[call-arg]
    return fn(text)  # type: ignore[call-arg]


def _infer_language(source_path: str | None) -> str:
    if not source_path:
        return "unknown"
    ext_map = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
        ".sh": "bash",
        ".sql": "sql",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".toml": "toml",
        ".md": "markdown",
    }
    for ext, lang in ext_map.items():
        if source_path.endswith(ext):
            return lang
    return "unknown"
