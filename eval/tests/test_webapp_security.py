"""Deterministic tests for `webapp/security.py` sensitive-value redaction."""

import pytest

from webapp.security import redact_sensitive


PLAINTEXT = "No secrets here; just ordinary customer prose."


def test_redact_sensitive_passes_through_plaintext() -> None:
    """Text without known secret patterns is returned unchanged."""
    assert redact_sensitive(PLAINTEXT) == PLAINTEXT


def test_redact_sensitive_handles_none_and_empty() -> None:
    """None and empty strings become empty strings."""
    assert redact_sensitive(None) == ""
    assert redact_sensitive("") == ""


@pytest.mark.parametrize(
    "text,expected_substring",
    [
        pytest.param(
            "ANTHROPIC_API_KEY=sk-ant-api03-verylongsecretvalue",
            "ANTHROPIC_API_KEY=***",
            id="anthropic_env_assignment",
        ),
        pytest.param(
            'api_key = "sk-ant-api03-anotherlongvalue"',
            'api_key = "***"',
            id="api_key_quoted",
        ),
        pytest.param(
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "Authorization: Bearer ***",
            id="authorization_bearer",
        ),
        pytest.param(
            "curl -H 'Authorization: Token ghp_1234567890abcdef1234567890abcdef123456'",
            "Authorization: Token ***",
            id="authorization_token_github",
        ),
        pytest.param(
            "ghp_1234567890abcdef1234567890abcdef123456",
            "***",
            id="github_pat_token",
        ),
        pytest.param(
            "github_pat_1234567890abcdef1234567890abcdef1234567890abcdef",
            "***",
            id="github_pat_long_token",
        ),
        pytest.param(
            "https://deploy:supersecret123@ci.example.com/repo.git",
            "https://<redacted>@ci.example.com/repo.git",
            id="url_with_credentials",
        ),
        pytest.param(
            "SALESFORCE_REFRESH_TOKEN=00Dxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "SALESFORCE_REFRESH_TOKEN=***",
            id="salesforce_token",
        ),
    ],
)
def test_redact_sensitive_redacts_known_secret(text: str, expected_substring: str) -> None:
    """Each known secret pattern is replaced while surrounding text survives."""
    result = redact_sensitive(text)
    assert expected_substring in result
    assert "supersecret" not in result
    assert "verylongsecretvalue" not in result
    assert "00Dxxxxxxxxxxxxxxxx" not in result
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result


def test_redact_sensitive_does_not_over_redact_short_values() -> None:
    """Short values that might be mistaken for secrets are left intact."""
    text = "api_key=abc api-secret=def PASSWORD=gh"
    assert redact_sensitive(text) == text


def test_redact_sensitive_redacts_multiple_secrets_in_one_string() -> None:
    """Several distinct secret patterns in the same string are all redacted."""
    text = (
        "ANTHROPIC_API_KEY=sk-ant-api03-abc123456789 "
        "https://user:pass@host/repo "
        "Authorization: Bearer token12345678"
    )
    result = redact_sensitive(text)
    assert "sk-ant-api03-abc123456789" not in result
    assert "user:pass" not in result
    assert "token12345678" not in result
    assert "ANTHROPIC_API_KEY=***" in result
    assert "https://<redacted>@host/repo" in result
    assert "Authorization: Bearer ***" in result
