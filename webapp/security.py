"""Small helpers for preventing accidental secret exposure in logs and UI output.

These functions are intentionally conservative: they redact well-known secret
patterns and credential-bearing URLs while leaving ordinary prose readable. They
do not replace proper secret storage; see `IMPLEMENTATION-PLAN.md` for the
longer-term keyring/encryption backlog.
"""

from __future__ import annotations

import re

_SECRET_MIN_LEN = 6

_REDACTION_PATTERNS = [
    # Authorization headers: Bearer / Token / Basic / ApiKey <value>
    (
        re.compile(
            r"(?i)(Authorization\s*[:=]\s*(?:Bearer|Token|Basic|ApiKey)\s+)[^\s'\"]+",
        ),
        r"\1***",
    ),
    # URL credentials: scheme://user:pass@host or scheme://token@host
    (
        re.compile(
            r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^\s@]+)?@",
        ),
        r"\1<redacted>@",
    ),
    # Anthropic API key value (sk-ant-...)
    (
        re.compile(r"\bsk-ant-[a-z0-9_-]{10,}\b", re.IGNORECASE),
        "***",
    ),
    # GitHub tokens: ghp_*, gho_*, ghe_*, ghu_*, ghs_*, ghr_*, github_pat_*
    (
        re.compile(
            r"\b(?:gh[pousr]_[a-z0-9]{36,}|github_pat_[a-z0-9_]{30,})\b",
            re.IGNORECASE,
        ),
        "***",
    ),
    # Explicit ANTHROPIC_API_KEY / api_key assignments
    (
        re.compile(
            r"(?i)(\b(?:ANTHROPIC_API_KEY|api[-_]?key)\s*=\s*)['\"]?[^\s'\"]{" + str(_SECRET_MIN_LEN) + r",}['\"]?",
        ),
        r"\1***",
    ),
    # Generic *_KEY / *_SECRET / *_TOKEN / *_PASSWORD assignments
    (
        re.compile(
            r"(?i)(\b[A-Za-z][A-Za-z0-9_]*_(?:KEY|SECRET|TOKEN|PASSWORD)\s*=\s*)['\"]?[^\s'\"]{" + str(_SECRET_MIN_LEN) + r",}['\"]?",
        ),
        r"\1***",
    ),
]


def redact_sensitive(text: str | None) -> str:
    """Return `text` with common secret patterns replaced by `***`.

    Redacts authorization headers, URL credentials, Anthropic/GitHub tokens, and
    environment-style secret assignments. Ordinary sentences without these
    patterns are returned unchanged.
    """
    if not text:
        return ""
    result = text
    for pattern, replacement in _REDACTION_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
