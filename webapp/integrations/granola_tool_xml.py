"""Bounded, deterministic reader for Granola MCP tool results delivered as XML text.

Gary's live ``--list-shape`` diagnostic showed ``list_meetings`` answering with one
text block of XML: an unrecognised root element holding ``<meeting>`` records that
each carry a ``<known_participants>`` element, plus one unrecognised sibling. This
module turns that shape into the same plain dictionaries the JSON path produces so
``GranolaRetrievalService`` validates both identically. It is not a general XML
mapper: only allow-listed element/attribute names are read, ``<meeting>`` records
without a validated UUID ``id`` are dropped and counted, unknown names are ignored
and counted, DTDs/entity declarations are refused before parsing, and depth and
element counts are capped. Nothing is inferred from the root element's name.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

MAX_XML_DEPTH = 6
MAX_XML_ELEMENTS = 20_000
MAX_XML_RECORDS = 1_000
MAX_XML_PARTICIPANTS = 500

_DECLARATION = re.compile(r"<!(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)", re.IGNORECASE)
_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

RECORD_ELEMENT = "meeting"
PARTICIPANTS_ELEMENT = "known_participants"
PARTICIPANT_ELEMENT = "participant"
WRAPPER_FIELDS = frozenset({"count", "from", "to"})
FIELD_NAMES = frozenset(
    {
        "id", "title", "date", "url", "summary", "transcript", "created_at",
        "captured_by_me", "listed_as_participant", "is_workspace_visible",
        "recording_context", "description", "audio_sources", "recorder",
        "microphone_sharing", "name", "email", "note_access_scope",
    }
)

XmlIssue = str  # "dtd_rejected" | "malformed_xml" | "too_deep" | "too_many_elements" | "too_many_records" | "ambiguous_field" | "mixed_content"


class GranolaXmlIssue(ValueError):
    def __init__(self, issue: XmlIssue) -> None:
        self.issue = issue
        super().__init__(issue)


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _check_bounds(root: ET.Element) -> None:
    count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > MAX_XML_ELEMENTS:
            raise GranolaXmlIssue("too_many_elements")
        if depth > MAX_XML_DEPTH:
            raise GranolaXmlIssue("too_deep")
        stack.extend((child, depth + 1) for child in element)


def _text(element: ET.Element) -> str:
    return (element.text or "").strip()


def _has_text(element: ET.Element) -> bool:
    return bool(_text(element)) or any((child.tail or "").strip() for child in element)


def _scalar(value: str) -> Any:
    return int(value) if value.isdigit() else value


def _set(out: dict[str, Any], key: str, value: Any) -> None:
    if key in out:
        raise GranolaXmlIssue("ambiguous_field")
    out[key] = value


def _participants(element: ET.Element, ignored: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    children = list(element)
    if children:
        for child in children:
            if _local(child.tag) == PARTICIPANT_ELEMENT:
                rows.append(_record(child, ignored))
            else:
                ignored[0] += 1
    else:
        for piece in re.split(r"[\n,;]+", _text(element)):
            piece = piece.strip()
            if piece:
                rows.append({"name": piece})
    if len(rows) > MAX_XML_PARTICIPANTS:
        raise GranolaXmlIssue("too_many_elements")
    return rows


def _record(element: ET.Element, ignored: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in element.attrib.items():
        local = _local(name)
        if local in FIELD_NAMES or local in WRAPPER_FIELDS:
            _set(out, local, _scalar(value))
        else:
            ignored[0] += 1
    children = list(element)
    if children and _has_text(element):
        raise GranolaXmlIssue("mixed_content")
    for child in children:
        local = _local(child.tag)
        if local == PARTICIPANTS_ELEMENT:
            _set(out, local, _participants(child, ignored))
        elif local in FIELD_NAMES:
            _set(out, local, _record(child, ignored) if len(child) else _scalar(_text(child)))
        else:
            ignored[0] += 1
    return out


def parse_tool_xml(text: str) -> dict[str, Any]:
    """Map one XML tool result to the dictionaries the JSON path yields.

    A root holding ``<meeting>`` children becomes ``{"meetings": [...], "count"/"from"/"to"
    from root attributes, "rejected_rows": n, "ignored_nodes": n}``; records lacking a
    UUID ``id`` are dropped into ``rejected_rows``. Any other root becomes one record
    (``get_meeting_transcript``/``get_account_info`` style). Raises ``GranolaXmlIssue``
    with a value-free issue code for declarations, malformed XML, exceeded bounds,
    duplicate field definitions or text mixed with child elements.
    """
    if _DECLARATION.search(text):
        raise GranolaXmlIssue("dtd_rejected")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise GranolaXmlIssue("malformed_xml") from exc
    _check_bounds(root)
    ignored = [0]
    records = [child for child in root if _local(child.tag) == RECORD_ELEMENT]
    if not records:
        out = _record(root, ignored)
        out["ignored_nodes"] = ignored[0]
        return out
    if len(records) > MAX_XML_RECORDS:
        raise GranolaXmlIssue("too_many_records")
    out = {}
    for name, value in root.attrib.items():
        local = _local(name)
        if local in WRAPPER_FIELDS:
            _set(out, local, _scalar(value))
        else:
            ignored[0] += 1
    meetings: list[dict[str, Any]] = []
    rejected = 0
    for record in records:
        row = _record(record, ignored)
        if isinstance(row.get("id"), str) and _UUID.match(row["id"]):
            meetings.append(row)
        else:
            rejected += 1
    ignored[0] += len(root) - len(records)
    out["meetings"] = meetings
    out["rejected_rows"] = rejected
    out["ignored_nodes"] = ignored[0]
    return out
