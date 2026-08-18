"""Pure verification of the hosted sandbox image supply chain.

The verifier deliberately accepts already-collected artifact facts and an
injected command runner. It never contacts a registry, invokes a shell, or
logs command output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class SupplyChainCommandRunner(Protocol):
    """Run a fixed verification command without exposing its output."""

    def run(self, argv: Sequence[str]) -> "SupplyChainCommandResult":
        ...


@dataclass(frozen=True)
class SupplyChainCommandResult:
    """Redacted result of an injected command invocation."""

    returncode: int
    stdout: str = ""


class SupplyChainArtifacts(BaseModel):
    """Facts collected from an image, its attestations, and its rootfs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_digest: str | None = None
    approved_image_digest: str | None = None
    sbom_text: str | None = None
    sbom_image_digest: str | None = None
    provenance_image_digest: str | None = None
    rootfs_digest: str | None = None
    manifest_rootfs_digest: str | None = None
    signature_command: tuple[str, ...] = ()
    signature_required: bool = True


class SupplyChainCheck(BaseModel):
    """One redacted supply-chain verification result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    ok: bool
    detail: str


class SupplyChainVerification(BaseModel):
    """Complete supply-chain verification result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[SupplyChainCheck, ...] = Field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def verify_supply_chain(
    artifacts: SupplyChainArtifacts,
    command_runner: SupplyChainCommandRunner,
) -> SupplyChainVerification:
    """Verify all required image, attestation, signature, and rootfs invariants."""
    checks = [
        _check_digest(artifacts),
        _check_sbom(artifacts),
        _check_provenance(artifacts),
        _check_signature(artifacts, command_runner),
        _check_rootfs(artifacts),
    ]
    return SupplyChainVerification(checks=tuple(checks))


def _check_digest(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    if not artifacts.image_digest or not artifacts.approved_image_digest:
        return SupplyChainCheck(
            check_id="image_digest",
            ok=False,
            detail="image digest or approved digest is missing",
        )
    if artifacts.image_digest != artifacts.approved_image_digest:
        return SupplyChainCheck(
            check_id="image_digest",
            ok=False,
            detail="image digest does not match approved digest",
        )
    return SupplyChainCheck(check_id="image_digest", ok=True, detail="digest matches approved pin")


def _check_sbom(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    if not artifacts.sbom_text:
        return SupplyChainCheck(check_id="sbom", ok=False, detail="SBOM is missing")
    try:
        parsed = json.loads(artifacts.sbom_text)
    except json.JSONDecodeError:
        return SupplyChainCheck(check_id="sbom", ok=False, detail="SBOM is not valid JSON")
    if not isinstance(parsed, dict):
        return SupplyChainCheck(check_id="sbom", ok=False, detail="SBOM root is not an object")
    if not artifacts.sbom_image_digest and not _contains_value(parsed, artifacts.image_digest):
        return SupplyChainCheck(check_id="sbom", ok=False, detail="SBOM image digest is missing")
    if artifacts.sbom_image_digest and artifacts.sbom_image_digest != artifacts.image_digest:
        return SupplyChainCheck(
            check_id="sbom",
            ok=False,
            detail="SBOM does not cover the verified image digest",
        )
    return SupplyChainCheck(check_id="sbom", ok=True, detail="SBOM parses and covers image digest")


def _contains_value(value: object, expected: str | None) -> bool:
    if expected is None:
        return False
    if value == expected:
        return True
    if isinstance(value, Mapping):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_value(item, expected) for item in value)
    return False


def _check_provenance(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    if not artifacts.provenance_image_digest:
        return SupplyChainCheck(check_id="provenance", ok=False, detail="provenance is missing")
    if artifacts.provenance_image_digest != artifacts.image_digest:
        return SupplyChainCheck(
            check_id="provenance",
            ok=False,
            detail="provenance does not match the verified image digest",
        )
    return SupplyChainCheck(check_id="provenance", ok=True, detail="provenance matches image digest")


def _check_signature(
    artifacts: SupplyChainArtifacts,
    command_runner: SupplyChainCommandRunner,
) -> SupplyChainCheck:
    if not artifacts.signature_required and not artifacts.signature_command:
        return SupplyChainCheck(check_id="signature", ok=True, detail="signature not required")
    if not artifacts.signature_command:
        return SupplyChainCheck(check_id="signature", ok=False, detail="signature verification command is missing")
    result = command_runner.run(artifacts.signature_command)
    if result.returncode != 0:
        return SupplyChainCheck(check_id="signature", ok=False, detail="signature verification failed")
    return SupplyChainCheck(check_id="signature", ok=True, detail="signature verified")


def _check_rootfs(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    if not artifacts.rootfs_digest or not artifacts.manifest_rootfs_digest:
        return SupplyChainCheck(check_id="rootfs_digest", ok=False, detail="rootfs digest or manifest is missing")
    if artifacts.rootfs_digest != artifacts.manifest_rootfs_digest:
        return SupplyChainCheck(
            check_id="rootfs_digest",
            ok=False,
            detail="rootfs digest does not match the approved-image manifest",
        )
    return SupplyChainCheck(check_id="rootfs_digest", ok=True, detail="rootfs matches approved-image manifest")
