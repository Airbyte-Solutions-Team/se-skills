"""Unit tests for assertion helpers and the mock output builder."""

from __future__ import annotations

import pytest

from eval.assertions import (
    SafeExpressionEvaluator,
    contains_any_case_insensitive,
    contains_case_insensitive,
    has_section,
    section_contains,
)
from eval.runner import MockOutputBuilder
from eval.schemas.manifest import Manifest


@pytest.mark.parametrize(
    "text,needle,expected",
    [
        ("Hello World", "hello", True),
        ("Hello World", "WORLD", True),
        ("Hello World", "goodbye", False),
        ("", "hello", False),
    ],
    ids=["lowercase", "uppercase", "missing", "empty_text"],
)
def test_contains_case_insensitive(text: str, needle: str, expected: bool) -> None:
    assert contains_case_insensitive(text, needle) is expected


@pytest.mark.parametrize(
    "text,needles,expected",
    [
        ("Hello World", ["hello", "goodbye"], True),
        ("Hello World", ["foo", "bar"], False),
        ("", ["foo"], False),
    ],
    ids=["one_match", "no_match", "empty_text"],
)
def test_contains_any_case_insensitive(
    text: str, needles: list[str], expected: bool
) -> None:
    assert contains_any_case_insensitive(text, needles) is expected


def test_has_section() -> None:
    markdown = "## At a Glance\nfoo\n## Source Coverage\nbar"
    assert has_section(markdown, "At a Glance") is True
    assert has_section(markdown, "Missing Section") is False


def test_section_contains() -> None:
    markdown = (
        "## Ranked Next Moves\n"
        "1. Run `account-refresher`\n"
        "2. Plan a discovery call\n"
        "## Source Coverage\n"
        "- transcript"
    )
    assert section_contains(markdown, "Ranked Next Moves", "account-refresher") is True
    assert section_contains(markdown, "Ranked Next Moves", "poc-plan") is False
    assert section_contains(markdown, "Missing Section", "anything") is False


def test_safe_expression_evaluator_with_env() -> None:
    output = "BYOK is not supported on any currently offered shape."
    env = {"airbyte_platform_available": False}
    manifest = {"id": "test", "title": "Test", "skills_under_test": ["deployment-model-qual"]}
    expression = (
        "not contains_any_case_insensitive(output, ['byok is supported', 'flex supports byok'])"
    )
    evaluator = SafeExpressionEvaluator(output=output, manifest=manifest, env=env)
    assert evaluator.evaluate(expression) is True


def test_safe_expression_evaluator_when_clause() -> None:
    output = "Entitlement could not be verified."
    env = {"airbyte_platform_available": False}
    manifest = {"id": "test", "title": "Test", "skills_under_test": ["tech-qual"]}
    expression = "contains_case_insensitive(output, 'verified')"
    when = "env['airbyte_platform_available'] is not True"
    evaluator = SafeExpressionEvaluator(output=output, manifest=manifest, env=env)
    assert evaluator.evaluate(when) is True
    assert evaluator.evaluate(expression) is True


def test_safe_expression_rejects_unsafe_code() -> None:
    evaluator = SafeExpressionEvaluator(output="", manifest={}, env={})
    with pytest.raises(Exception):
        evaluator.evaluate("__import__('os').system('ls')")


def test_mock_output_builder_respects_constraints() -> None:
    manifest = Manifest(
        id="phase1-hourly-sync-constraint",
        title="Hourly sync constraint",
        skills_under_test=["tech-qual"],
        customer_constraints=["Hourly synchronization is required."],
        forbidden_behavior=["Do not recommend reducing sync frequency"],
        expected_sections=["At a Glance", "Source Coverage"],
        per_skill_expected_sections={"tech-qual": ["Data Volume & Scale"]},
    )
    builder = MockOutputBuilder(manifest, "tech-qual", env={}, account="Acme")
    output = builder.build()

    assert has_section(output, "At a Glance")
    assert has_section(output, "Source Coverage")
    assert has_section(output, "Data Volume & Scale")
    assert "reduce frequency" not in output.lower()
    assert "hourly" in output.lower()
