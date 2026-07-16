"""Deterministic tests for external-input boundaries and output encoding.

These tests focus on enforceable limits (Pydantic `max_length`) and output encoding
(`OutputService._html_escape`, `OutputService._upsert_handover_card`) rather than
natural-language prompt sanitization, which is intentionally out of scope.
"""

import pytest
from pydantic import ValidationError

from integrations.salesforce import SalesforceIntegration
from webapp.config import _safe, _titlecase_folder
from webapp.routes.skills import InvokeBody
from webapp.routes.transcription import AskLive, StartLive
from webapp.routes.ask import OutputAsk
from webapp.routes.outputs import OutputPdf, PushToRepo
from webapp.services.output_service import OutputService


def _svc() -> OutputService:
    return OutputService(
        customers_dir=None,
        workspace=None,
        repo_root=None,
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n,
        run_cmd=None,
        internal_repo=None,
    )


class TestModelBoundaries:
    """Pydantic models reject unreasonably large free-form inputs."""

    def test_push_to_repo_path_length(self) -> None:
        with pytest.raises(ValidationError):
            PushToRepo(path="x" * 501, account="Acme")

    def test_push_to_repo_meta_length(self) -> None:
        with pytest.raises(ValidationError):
            PushToRepo(path="ok", account="Acme", meta="x" * 1001)

    def test_output_pdf_append_md_length(self) -> None:
        with pytest.raises(ValidationError):
            OutputPdf(path="ok", append_md="x" * 20_001)

    def test_output_ask_question_length(self) -> None:
        with pytest.raises(ValidationError):
            OutputAsk(path="ok", question="x" * 5001)

    def test_invoke_body_freeform_length(self) -> None:
        with pytest.raises(ValidationError):
            InvokeBody(account="Acme", freeform="x" * 20_001)

    def test_invoke_body_extra_length(self) -> None:
        with pytest.raises(ValidationError):
            InvokeBody(account="Acme", extra="x" * 10_001)

    def test_start_live_account_length(self) -> None:
        with pytest.raises(ValidationError):
            StartLive(account="x" * 121, mic_device=0)

    def test_ask_live_question_length(self) -> None:
        with pytest.raises(ValidationError):
            AskLive(question="x" * 5001)

    def test_models_accept_normal_input(self) -> None:
        PushToRepo(path="account/outputs/handover.html", account="Acme", meta="Active")
        OutputPdf(path="account/outputs/foo.md", append_md="# Q&A\n\nHello")
        OutputAsk(path="account/outputs/foo.md", question="What is the next move?")
        InvokeBody(account="Acme", skill="next-move", freeform="Tell me what to do.")
        StartLive(account="Acme", mic_device=0)
        AskLive(question="Summarize the last five minutes")


def test_html_escape_encodes_markup_characters() -> None:
    """`_html_escape` turns HTML metacharacters into entities."""
    assert OutputService._html_escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_safe_rejects_path_metacharacters() -> None:
    """`_safe` blocks names that could escape the customer directory."""
    with pytest.raises(Exception):  # HTTPException
        _safe("../etc")
    with pytest.raises(Exception):
        _safe("a/b")


def test_safe_accepts_normal_names() -> None:
    assert _safe("Acme-Co") == "Acme-Co"


def test_titlecase_folder_normalizes_punctuation() -> None:
    """SFDC account names with punctuation become safe folder slugs."""
    assert _titlecase_folder("Octus (fka Reorg Research)") == "Octus-Fka-Reorg-Research"


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9-]+", "-", (name or "").strip()).strip("-") or "opportunity"


def test_sf_quote_uses_soql_string_literal() -> None:
    """`_quote` delegates to the SOQL helper for `=`/`IN` literals."""
    assert SalesforceIntegration._quote("O'Brien") == r"O\'Brien"
    assert SalesforceIntegration._quote('A"B') == r'A\"B'  # backslash-escaped double quote


def test_sfdc_like_prefix_uses_soql_like_prefix(tmp_path, monkeypatch) -> None:
    """`_sfdc_like_prefix` uses the stored real SFDC name and escapes LIKE wildcards."""
    customers = tmp_path / "customers"
    account_dir = customers / "Acme"
    account_dir.mkdir(parents=True)
    (account_dir / ".sfdc-name").write_text("50%_Acme")
    sf = SalesforceIntegration(
        customers_dir=customers,
        workspace=tmp_path,
        sf_config=lambda: {},
        titlecase=_titlecase_folder,
        slug=_slug,
    )
    assert sf._sfdc_like_prefix("Acme") == r"50\%\_Acme"


def test_sfdc_like_prefix_falls_back_to_first_token(tmp_path, monkeypatch) -> None:
    """When no `.sfdc-name` exists, `_sfdc_like_prefix` falls back to the first token."""
    sf = SalesforceIntegration(
        customers_dir=tmp_path / "customers",
        workspace=tmp_path,
        sf_config=lambda: {},
        titlecase=_titlecase_folder,
        slug=_slug,
    )
    assert sf._sfdc_like_prefix("Acme-Co") == "Acme"


def test_upsert_handover_card_escapes_meta_and_account() -> None:
    """User-supplied `meta` and account names are HTML-escaped in the card."""
    base = '<div class="nav-grid">\n</div>'
    account = 'Acme <script>alert(1)</script>'
    description = 'A <b>test</b> description'
    meta = '<img src=x onerror=alert(1)>'
    account_slug = _titlecase_folder(account).lower()
    svc = _svc()
    result = svc._upsert_handover_card(base, account, account_slug, description, meta)

    assert "<script>" not in result
    assert "<img" not in result
    assert "&lt;script&gt;" in result
    assert "&lt;img" in result
    assert "<b>test</b>" not in result
    assert "&lt;b&gt;test&lt;/b&gt;" in result
    assert "Acme" in result
