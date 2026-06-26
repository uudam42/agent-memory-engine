"""Deterministic proposition extractor — Phase 10.

Extracts atomic factual or behavioral statements from source content without
requiring an LLM.  Every proposition is independently useful and traceable
to a source span.

Extraction sources (in order of confidence):
  Python/code:
    - Module/class/function docstrings (first meaningful sentence)
    - Comments containing constraint/security keywords
    - Raise statements (risks)
    - Function signatures with known patterns
  Markdown/docs:
    - Bullet-point and numbered-list items
    - Sentences containing constraint keywords (must, cannot, never, always)
    - Headings with their direct following sentence

Proposition types:
  behavior            — what something does / returns / accepts
  constraint          — what must / must not happen
  architecture        — structural or design facts
  security_rule       — security-relevant rules (shell=False, allowlist, etc.)
  implementation_detail — how something is implemented
  decision            — why a choice was made
  procedure           — step-by-step workflow
  risk                — what can go wrong / what raises exceptions
  test_evidence       — test-derived facts
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RawProposition:
    proposition_text: str
    normalized_text: str
    proposition_type: str
    confidence: float
    source_start_line: int | None = None
    source_end_line: int | None = None
    parent_paragraph_index: int | None = None  # index into RawParagraph list

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.normalized_text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Keyword patterns for type classification
# ---------------------------------------------------------------------------

_SECURITY_PATTERNS = re.compile(
    r"shell\s*=\s*False|allowlist|blocklist|deny.?list|sanitiz|redact|"
    r"credential|secret|token|auth(?:entication|orization)|permission|"
    r"injection|XSS|SQL.inject|CSRF|path.traversal|symlink",
    re.I,
)
_CONSTRAINT_PATTERNS = re.compile(
    r"\b(?:must(?:\s+not)?|cannot|can't|never|always|required|"
    r"forbidden|prohibited|shall(?:\s+not)?|do\s+not|don't)\b",
    re.I,
)
_ARCHITECTURE_PATTERNS = re.compile(
    r"\b(?:architect(?:ure)?|design\s+pattern|separation|interface|"
    r"abstraction|layer|boundary|contract|invariant|schema|protocol)\b",
    re.I,
)
_DECISION_PATTERNS = re.compile(
    r"\b(?:decided?|chose?|using\s+\w+\s+instead|replaced?|migrat(?:ed?|ing)|"
    r"rationale|reason\s+(?:for|we)|prefer(?:red?)?)\b",
    re.I,
)
_RISK_PATTERNS = re.compile(
    r"\braises?\b|\bthrows?\b|\bexcept(?:ion)?\b|\bfails?\b|\bpanic\b|"
    r"\bdegrade\b|\bfall.?back\b",
    re.I,
)
_TEST_PATTERNS = re.compile(
    r"\bassert\b|\bverif(?:y|ies|ied)\b|\btest\b|\bexpect(?:ed)?\b|\bshould\b",
    re.I,
)
_PROCEDURE_PATTERNS = re.compile(
    r"\bstep\s+\d|how\s+to\b|\bworkflow\b|\bprocedure\b|\bprocess\b|"
    r"first\s*,?\s*then|\brun\b|\bexecute\b|\binvoke\b",
    re.I,
)

_DOCSTRING_QUOTE_RE = re.compile(r'"""(.*?)"""|\'\'\'(.*?)\'\'\'', re.DOTALL)
_SINGLE_LINE_COMMENT_RE = re.compile(r"^\s*#\s*(.+)")
_RAISE_RE = re.compile(r"^\s*raise\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+)", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+)", re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# Keywords that mark a line as high-value for extraction
_HIGH_VALUE_COMMENT_KEYWORDS = re.compile(
    r"shell\s*=\s*False|allowlist|blocklist|must\s+not|never|always|"
    r"invariant|constraint|security|important|critical|note:",
    re.I,
)


def _classify_type(text: str) -> tuple[str, float]:
    """Return (proposition_type, confidence_delta) based on text content."""
    if _SECURITY_PATTERNS.search(text):
        return "security_rule", 0.15
    if _CONSTRAINT_PATTERNS.search(text):
        return "constraint", 0.10
    if _ARCHITECTURE_PATTERNS.search(text):
        return "architecture", 0.05
    if _DECISION_PATTERNS.search(text):
        return "decision", 0.05
    if _RISK_PATTERNS.search(text):
        return "risk", 0.05
    if _TEST_PATTERNS.search(text):
        return "test_evidence", 0.0
    if _PROCEDURE_PATTERNS.search(text):
        return "procedure", 0.0
    return "implementation_detail", 0.0


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _is_worthless(text: str) -> bool:
    """Filter out non-propositions: too short, pure code, imports, etc."""
    t = text.strip()
    if len(t) < 12:
        return True
    # Pure import lines
    if re.match(r"^(?:import|from)\s+\w", t):
        return True
    # Pure ellipsis or pass
    if t in ("...", "pass", "None", "True", "False"):
        return True
    # Only punctuation / numbers
    if re.match(r"^[\s\d.,;:!?()-]+$", t):
        return True
    # Very long — more than 350 chars → not atomic
    if len(t) > 350:
        return True
    return False


def _first_sentence(text: str) -> str:
    """Extract the first meaningful sentence from a block of text."""
    text = text.strip()
    # Strip leading decorators or empty lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cleaned = " ".join(lines)
    parts = _SENTENCE_END_RE.split(cleaned, maxsplit=1)
    return parts[0].strip()


# ---------------------------------------------------------------------------
# Python / code extraction
# ---------------------------------------------------------------------------


def _extract_from_docstrings(
    text: str,
    lines: list[str],
) -> list[RawProposition]:
    """Extract first meaningful sentence from each docstring."""
    props: list[RawProposition] = []
    for m in _DOCSTRING_QUOTE_RE.finditer(text):
        raw = (m.group(1) or m.group(2) or "").strip()
        if not raw:
            continue
        first = _first_sentence(raw)
        if _is_worthless(first):
            continue
        ptype, delta = _classify_type(first)
        norm = _normalize(first)
        # Find line number
        start_pos = m.start()
        start_line = text[:start_pos].count("\n") + 1
        props.append(RawProposition(
            proposition_text=first,
            normalized_text=norm,
            proposition_type=ptype,
            confidence=min(0.85 + delta, 0.99),
            source_start_line=start_line,
            source_end_line=start_line,
        ))
    return props


def _extract_from_comments(
    lines: list[str],
) -> list[RawProposition]:
    """Extract high-value single-line comments."""
    props: list[RawProposition] = []
    for i, line in enumerate(lines, 1):
        m = _SINGLE_LINE_COMMENT_RE.match(line)
        if not m:
            continue
        content = m.group(1).strip()
        # Skip type-ignore, noqa, copyright, shebang
        if re.match(r"type:\s*ignore|noqa|copyright|#!|coding:", content, re.I):
            continue
        if not _HIGH_VALUE_COMMENT_KEYWORDS.search(content):
            continue
        if _is_worthless(content):
            continue
        ptype, delta = _classify_type(content)
        norm = _normalize(content)
        props.append(RawProposition(
            proposition_text=content,
            normalized_text=norm,
            proposition_type=ptype,
            confidence=min(0.78 + delta, 0.99),
            source_start_line=i,
            source_end_line=i,
        ))
    return props


def _extract_from_raises(text: str) -> list[RawProposition]:
    """Extract raise statements as risk propositions."""
    props: list[RawProposition] = []
    for m in _RAISE_RE.finditer(text):
        exc_type = m.group(1)
        exc_msg = m.group(2).strip().strip('"\'')[:120]
        if exc_msg:
            stmt = f"Raises {exc_type}: {exc_msg}"
        else:
            stmt = f"Raises {exc_type}"
        if _is_worthless(stmt):
            continue
        line = text[:m.start()].count("\n") + 1
        norm = _normalize(stmt)
        props.append(RawProposition(
            proposition_text=stmt,
            normalized_text=norm,
            proposition_type="risk",
            confidence=0.80,
            source_start_line=line,
            source_end_line=line,
        ))
    return props


def extract_from_code(
    text: str,
    source_path: str | None = None,
) -> list[RawProposition]:
    """Extract propositions from Python/code source."""
    lines = text.splitlines()
    props: list[RawProposition] = []

    props.extend(_extract_from_docstrings(text, lines))
    props.extend(_extract_from_comments(lines))
    props.extend(_extract_from_raises(text))

    return _deduplicate(props)


# ---------------------------------------------------------------------------
# Markdown / documentation extraction
# ---------------------------------------------------------------------------


def extract_from_markdown(
    text: str,
    source_path: str | None = None,
) -> list[RawProposition]:
    """Extract propositions from Markdown or documentation."""
    props: list[RawProposition] = []
    lines = text.splitlines()

    # 1. Bullet points — each bullet = one candidate proposition
    for m in _BULLET_RE.finditer(text):
        item = m.group(1).strip()
        if _is_worthless(item):
            continue
        first = _first_sentence(item)
        if _is_worthless(first):
            continue
        ptype, delta = _classify_type(first)
        line = text[:m.start()].count("\n") + 1
        norm = _normalize(first)
        props.append(RawProposition(
            proposition_text=first,
            normalized_text=norm,
            proposition_type=ptype,
            confidence=min(0.72 + delta, 0.95),
            source_start_line=line,
            source_end_line=line,
        ))

    # 2. Numbered list items
    for m in _NUMBERED_RE.finditer(text):
        item = m.group(1).strip()
        if _is_worthless(item):
            continue
        first = _first_sentence(item)
        if _is_worthless(first):
            continue
        ptype, delta = _classify_type(first)
        line = text[:m.start()].count("\n") + 1
        norm = _normalize(first)
        props.append(RawProposition(
            proposition_text=first,
            normalized_text=norm,
            proposition_type=ptype,
            confidence=min(0.70 + delta, 0.95),
            source_start_line=line,
            source_end_line=line,
        ))

    # 3. Constraint/security sentences anywhere in prose
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("#", "-", "*", "•")) or not stripped:
            continue  # already handled above
        if _CONSTRAINT_PATTERNS.search(stripped) or _SECURITY_PATTERNS.search(stripped):
            first = _first_sentence(stripped)
            if _is_worthless(first):
                continue
            ptype, delta = _classify_type(first)
            norm = _normalize(first)
            props.append(RawProposition(
                proposition_text=first,
                normalized_text=norm,
                proposition_type=ptype,
                confidence=min(0.68 + delta, 0.95),
                source_start_line=i,
                source_end_line=i,
            ))

    return _deduplicate(props)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_CODE_SOURCE_TYPES = frozenset({
    "code_file", "code_comment", "test_report",
})
_MARKDOWN_SOURCE_TYPES = frozenset({
    "markdown", "readme", "architecture_doc", "adr", "api_spec",
    "manual_note", "task_artifact",
})


def extract_propositions(
    text: str,
    source_type: str,
    source_path: str | None = None,
) -> list[RawProposition]:
    """Route proposition extraction based on source type."""
    if source_type in _CODE_SOURCE_TYPES:
        return extract_from_code(text, source_path)
    if source_type in _MARKDOWN_SOURCE_TYPES:
        return extract_from_markdown(text, source_path)
    # git_diff, runtime_log — minimal extraction
    return extract_from_markdown(text, source_path)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate(props: list[RawProposition]) -> list[RawProposition]:
    """Remove duplicate propositions by normalized_text hash."""
    seen: set[str] = set()
    result: list[RawProposition] = []
    for p in props:
        key = p.content_hash
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result
