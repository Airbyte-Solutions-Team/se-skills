"""Pydantic models for evaluation manifests.

Manifests describe a synthetic scenario, the skills to exercise, the evidence
available, and the deterministic assertions that must hold in the generated
outputs. They are intentionally plain YAML so non-engineers can add new SE
scenarios without touching Python.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Assertion(BaseModel):
    """A single deterministic check against a skill output."""

    name: str = Field(..., description="Human-readable assertion name.")
    target: str = Field("markdown", description="Output surface to check; currently only 'markdown'.")
    check: str = Field(..., description="Safe expression evaluated against the output text.")
    severity: Literal["blocker", "major", "minor"] = Field("blocker", description="Failure severity.")
    when: Optional[str] = Field(None, description="Optional safe expression; assertion applies only when true.")
    skills: Optional[List[str]] = Field(None, description="Limit assertion to these skills; if omitted, apply to all.")

    @field_validator("severity")
    @classmethod
    def severity_is_lowercase(cls, value: str) -> str:
        return value.lower()


class ModelJudge(BaseModel):
    """Optional LLM-as-judge configuration for a scenario."""

    enabled: bool = False
    criteria: List[str] = Field(default_factory=list)
    threshold: str = "all"


class FixtureItem(BaseModel):
    """A fixture file and its destination inside the temporary workspace."""

    source: str
    target: str


class Fixtures(BaseModel):
    """Synthetic inputs for a scenario."""

    transcripts: List[FixtureItem] = Field(default_factory=list)
    existing_outputs: List[FixtureItem] = Field(default_factory=list)
    config: str = Field("fixtures/config/synthetic-se-config.yaml", description="Path to the SE config fixture.")


class Environment(BaseModel):
    """Simulated external capabilities for a scenario."""

    reference_data: Dict[str, Any] = Field(default_factory=dict)
    salesforce: Union[bool, Dict[str, Any]] = Field(
        False,
        description="Whether Salesforce enrichment is available, or a dict of simulated SFDC values.",
    )


class Manifest(BaseModel):
    """A complete evaluation scenario."""

    manifest_version: str = "1.0"
    id: str
    title: str
    description: Optional[str] = None
    skills_under_test: List[str] = Field(..., alias="skills_under_test")
    tags: List[str] = Field(default_factory=list)
    fixtures: Fixtures = Field(default_factory=Fixtures)
    environment: Environment = Field(default_factory=Environment)
    customer_constraints: List[str] = Field(default_factory=list)
    available_evidence: List[str] = Field(default_factory=list)
    required_behavior: List[str] = Field(default_factory=list)
    forbidden_behavior: List[str] = Field(default_factory=list)
    expected_sections: List[str] = Field(default_factory=list)
    per_skill_expected_sections: Dict[str, List[str]] = Field(default_factory=dict)
    expected_refusal_for: List[str] = Field(default_factory=list)
    deterministic_assertions: List[Assertion] = Field(default_factory=list)
    model_judge: ModelJudge = Field(default_factory=ModelJudge)
    failure_severity: Literal["blocker", "major", "minor"] = "blocker"
    notes: Optional[str] = None

    @field_validator("failure_severity")
    @classmethod
    def failure_severity_is_lowercase(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def normalize_lists(self) -> "Manifest":
        """Ensure list fields are real lists and string fields are stripped."""
        for field_name in ["skills_under_test", "tags", "customer_constraints", "available_evidence", "required_behavior", "forbidden_behavior", "expected_sections", "expected_refusal_for"]:
            value = getattr(self, field_name)
            if value is None:
                setattr(self, field_name, [])
        return self

    @classmethod
    def from_yaml(cls, path: Path) -> "Manifest":
        """Load and validate a manifest from a YAML file."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def expected_sections_for_skill(self, skill: str) -> List[str]:
        """Return the global plus skill-specific expected section headings."""
        return list(dict.fromkeys(self.expected_sections + self.per_skill_expected_sections.get(skill, [])))
