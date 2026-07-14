"""Helpers for building SOQL string literals used by the webapp Salesforce integration.

These functions escape characters that would otherwise change query semantics or
break string-literal syntax. They do not validate field names or build full
queries — callers remain responsible for correct SOQL structure.
"""

from __future__ import annotations


def soql_string_literal(value: str) -> str:
    """Return `value` escaped for use inside a SOQL single-quoted string.

    Escapes backslashes, single quotes, and double quotes so the literal preserves
    the exact characters of the input. Use this for `=` and `IN` comparisons.
    """
    if not value:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
    )


def soql_like_prefix(value: str) -> str:
    r"""Return a SOQL LIKE prefix that matches the literal start of `value`.

    In addition to the string-literal escapes, this escapes the LIKE wildcards
    `%` and `_` and the SOQL escape character `\` so they are treated as
    literal characters rather than pattern operators. The result is safe to use
    in a `LIKE '<prefix>%'` clause.

    If `value` is empty the result is an empty string; callers should guard
    against using an empty prefix.
    """
    escaped = soql_string_literal(value)
    return escaped.replace("%", "\\%").replace("_", "\\_")
