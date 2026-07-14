"""Deterministic tests for the SOQL string-literal helpers in `webapp/soql.py`.

These tests verify that user-supplied account names are escaped correctly for
SOQL `=`/`IN` literals and for `LIKE` prefixes, without assuming a live
Salesforce connection.
"""

import pytest

from webapp.soql import soql_like_prefix, soql_string_literal


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param("Acme", "Acme", id="normal"),
        pytest.param("O'Reilly", r"O\'Reilly", id="apostrophe"),
        pytest.param(r"A\B", r"A\\B", id="backslash"),
        pytest.param('A"B', 'A\\\"B', id="double_quote"),
        pytest.param("50%_", "50%_", id="percent_underscore_unescaped"),
        pytest.param("ACME \ud83d\ude80", "ACME \ud83d\ude80", id="unicode"),
        pytest.param("", "", id="empty"),
    ],
)
def test_soql_string_literal_escapes(value: str, expected: str) -> None:
    """`soql_string_literal` escapes single quotes, double quotes, and backslashes."""
    assert soql_string_literal(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param("Acme", "Acme", id="normal"),
        pytest.param("O'Reilly", r"O\'Reilly", id="apostrophe"),
        pytest.param("50% Acme", r"50\% Acme", id="percent"),
        pytest.param("A_B", r"A\_B", id="underscore"),
        pytest.param(r"A\B", r"A\\B", id="backslash"),
        pytest.param("50%' OR '1'='1", r"50\%\' OR \'1\'=\'1", id="injection_attempt"),
        pytest.param("ACME \ud83d\ude80", "ACME \ud83d\ude80", id="unicode"),
        pytest.param("", "", id="empty"),
    ],
)
def test_soql_like_prefix_escapes_wildcards(value: str, expected: str) -> None:
    """`soql_like_prefix` also escapes `%` and `_` so they are literal in LIKE."""
    assert soql_like_prefix(value) == expected


def test_soql_like_prefix_never_leaves_bare_wildcards() -> None:
    """For a prefix containing wildcards, every `%` and `_` is escaped."""
    prefix = soql_like_prefix("50%_danger")
    # The only `%` and `_` in the result must be preceded by a backslash.
    for i, ch in enumerate(prefix):
        if ch in {"%", "_"}:
            assert i > 0 and prefix[i - 1] == "\\", f"unescaped {ch} at position {i}"
