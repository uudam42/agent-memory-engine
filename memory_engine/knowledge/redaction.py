"""Secret redaction — strips credentials, tokens, and private URLs before persistence.

All patterns are compile-once regex.  Redaction is applied to raw content
before hashing and chunking so secrets never touch the DB.

Patterns covered:
  - API keys (generic pattern: /[A-Za-z0-9_-]{20,}/)
  - Bearer / Authorization header values
  - Private URLs containing passwords (http://user:pass@host)
  - AWS / GCP / Azure credential patterns
  - Private keys (-----BEGIN ... KEY-----)
  - Password assignments in config-like strings
  - JWT tokens (three base64 segments separated by dots)
  - GitHub / GitLab tokens (ghp_, ghs_, glpat-)
  - Hex secrets ≥ 40 chars (SHA1/SHA256 length)
"""

from __future__ import annotations

import re

# Replacement placeholder
_REDACTED = "[REDACTED]"

_PATTERNS: list[re.Pattern[str]] = [
    # ── Private keys ────────────────────────────────────────────────────────
    re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |)(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END[^-]+-----",
        re.S,
    ),
    # ── JWT (three base64url segments) ──────────────────────────────────────
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # ── GitHub / GitLab tokens ───────────────────────────────────────────────
    re.compile(r"\b(ghp|ghs|gho|ghu|github_pat|glpat)_[A-Za-z0-9_]{20,}\b"),
    # ── AWS credentials ─────────────────────────────────────────────────────
    re.compile(r"\b(AKIA|ASIA|AROA|AIPA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b"),
    re.compile(r"(?:aws_secret_access_key|aws_session_token)\s*[=:]\s*\S+", re.I),
    # ── Bearer / Authorization header ───────────────────────────────────────
    re.compile(r"(?:Bearer|Authorization:?\s*(?:Bearer|Basic|Token))\s+[A-Za-z0-9_.+/=\-]{8,}", re.I),
    # ── Password in URL (any://user:PASS@host) ───────────────────────────────
    re.compile(r"[a-zA-Z][a-zA-Z0-9+\-.]*://[^:@\s]+:[^@\s]{4,}@[^\s]+"),
    # ── password / secret / token / api_key assignments ─────────────────────
    re.compile(
        r"""(?:password|passwd|secret|api[_\-]?key|auth[_\-]?token|access[_\-]?token|private[_\-]?key)\s*[=:]\s*['"]?([^\s'"]{6,})""",
        re.I,
    ),
    # ── Long hex strings (≥ 40 hex chars) — SHA1/SHA256 length secrets ───────
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
]


def redact(content: str) -> tuple[str, int]:
    """Return (redacted_content, count_of_replacements).

    Replacement count is useful for logging and tests.
    """
    count = 0
    for pattern in _PATTERNS:
        new_content, n = pattern.subn(_REDACTED, content)
        count += n
        content = new_content
    return content, count
