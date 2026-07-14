"""Deterministic assertion helpers and a safe expression evaluator.

Assertions operate on the text of a skill output. They can check phrase
presence, section existence, and simple boolean combinations of those
checks. The expression evaluator is intentionally restricted: it can only
call the whitelisted helpers and reference the `output`, `manifest`, and
`env` variables.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Sequence, Tuple


SectionResult = Tuple[bool, str]


def contains_case_insensitive(text: str, needle: str) -> bool:
    """Return True when `needle` appears anywhere in `text` (case-insensitive)."""
    return needle.lower() in text.lower()


def contains_any_case_insensitive(text: str, needles: Sequence[str]) -> bool:
    """Return True when any of `needles` appears in `text` (case-insensitive)."""
    return any(contains_case_insensitive(text, needle) for needle in needles)


def contains_all_case_insensitive(text: str, needles: Sequence[str]) -> bool:
    """Return True when all of `needles` appear in `text` (case-insensitive)."""
    return all(contains_case_insensitive(text, needle) for needle in needles)


def _find_heading_lines(markdown: str, heading: str) -> List[Tuple[int, int]]:
    """Return all (line_index, depth) pairs matching `heading` (case-insensitive)."""
    escaped = re.escape(heading)
    pattern = re.compile(r"^(#{1,6})\s*" + escaped + r"\s*$", re.MULTILINE | re.IGNORECASE)
    results: List[Tuple[int, int]] = []
    for match in pattern.finditer(markdown):
        start = match.start()
        line_index = markdown[:start].count("\n")
        depth = len(match.group(1))
        results.append((line_index, depth))
    return results


def has_section(markdown: str, heading: str) -> bool:
    """Return True when `markdown` contains a heading matching `heading`."""
    return bool(_find_heading_lines(markdown, heading))


def extract_section(markdown: str, heading: str) -> str:
    """Extract the text under the first matching heading.

    Extraction stops at the next heading of equal or higher precedence
    (fewer `#` characters). Returns an empty string if the heading is not
    found.
    """
    headings = _find_heading_lines(markdown, heading)
    if not headings:
        return ""

    line_index, depth = headings[0]
    lines = markdown.splitlines()
    if line_index >= len(lines) - 1:
        return ""

    body: List[str] = []
    for line in lines[line_index + 1 :]:
        heading_match = re.match(r"^(#{1,6})\s+", line)
        if heading_match:
            next_depth = len(heading_match.group(1))
            if next_depth <= depth:
                break
        body.append(line)

    return "\n".join(body).strip()


def section_contains(markdown: str, heading: str, phrase: str) -> bool:
    """Return True when `phrase` appears inside the named section."""
    section = extract_section(markdown, heading)
    return contains_case_insensitive(section, phrase)


class UnsafeExpressionError(Exception):
    """Raised when an assertion expression uses disallowed syntax."""


def section_contains_any_case_insensitive(markdown: str, heading: str, phrases: Sequence[str]) -> bool:
    """Return True when any `phrase` appears inside the named section."""
    section = extract_section(markdown, heading)
    return any(contains_case_insensitive(section, phrase) for phrase in phrases)


class SafeExpressionEvaluator:
    """Evaluate a small boolean expression against an assertion context."""

    _allowed_functions = {
        "contains_case_insensitive": contains_case_insensitive,
        "contains_any_case_insensitive": contains_any_case_insensitive,
        "contains_all_case_insensitive": contains_all_case_insensitive,
        "has_section": has_section,
        "section_contains": section_contains,
        "section_contains_any_case_insensitive": section_contains_any_case_insensitive,
        "extract_section": extract_section,
    }

    _allowed_op_types = {
        ast.And,
        ast.Or,
        ast.Not,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Is,
        ast.IsNot,
    }

    def __init__(self, *, output: str, manifest: Any, env: Dict[str, Any]) -> None:
        self.output = output
        self.manifest = manifest
        self.env = env
        self.names: Dict[str, Any] = {
            "output": output,
            "manifest": manifest,
            "env": env,
            "true": True,
            "false": False,
            "none": None,
        }

    def evaluate(self, expression: str) -> bool:
        """Return the boolean result of `expression`.

        Raises `UnsafeExpressionError` if the expression contains disallowed
        nodes, and propagates `ValueError` for malformed expressions.
        """
        if not expression or not expression.strip():
            raise ValueError("Empty assertion expression")
        tree = ast.parse(expression.strip(), mode="eval")
        return bool(self._eval_node(tree.body))

    def _eval_node(self, node: ast.AST) -> Any:
        if isinstance(node, ast.BoolOp):
            values = [self._eval_node(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise UnsafeExpressionError(f"Unsupported boolean operator: {type(node.op).__name__}")

        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            raise UnsafeExpressionError(f"Unsupported unary operator: {type(node.op).__name__}")

        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = self._eval_node(right_node)
                if not self._compare(left, op, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name):
                raise UnsafeExpressionError("Only direct function calls are allowed")
            if func.id not in self._allowed_functions:
                raise UnsafeExpressionError(f"Function '{func.id}' is not allowed")
            args = [self._eval_node(a) for a in node.args]
            return self._allowed_functions[func.id](*args)

        if isinstance(node, ast.Name):
            if node.id not in self.names:
                raise UnsafeExpressionError(f"Name '{node.id}' is not allowed")
            return self.names[node.id]

        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval_node(elt) for elt in node.elts]

        if isinstance(node, ast.Subscript):
            value = self._eval_node(node.value)
            index = self._eval_node(node.slice)
            return value[index]

        raise UnsafeExpressionError(f"Disallowed expression node: {type(node).__name__}")

    def _compare(self, left: Any, op: ast.cmpop, right: Any) -> bool:
        if type(op) not in self._allowed_op_types:
            raise UnsafeExpressionError(f"Unsupported comparison: {type(op).__name__}")
        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.Is):
            return left is right
        if isinstance(op, ast.IsNot):
            return left is not right
        raise UnsafeExpressionError(f"Unhandled comparison: {type(op).__name__}")
