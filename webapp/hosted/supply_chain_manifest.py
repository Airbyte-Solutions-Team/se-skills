"""Load trusted, host-installed sandbox supply-chain evidence."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from webapp.hosted.supply_chain import SupplyChainArtifacts


class ManifestProbe(Protocol):
    """Host access needed to load an approved artifact manifest."""

    def path_info(self, path: str) -> "ManifestPathFacts":
        ...

    def file_text(self, path: str) -> str | None:
        ...


class ManifestPathFacts(Protocol):
    """Structural path facts required for trust validation."""

    exists: bool
    owner: str | None
    mode: int | None
    is_file: bool


class SignatureManifest(BaseModel):
    """Cosign verification expectations from the approved manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_reference: str
    certificate_identity: str | None = None
    certificate_oidc_issuer: str | None = None
    public_key_path: str | None = None
    certificate_path: str | None = None


class ArtifactManifest(BaseModel):
    """Host-installed pointers to approved image evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_digest: str
    rootfs_digest: str
    sbom_path: str
    provenance_path: str
    signature: SignatureManifest


class ManifestLoadResult(BaseModel):
    """Redacted result of loading and trusting an artifact manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifacts: SupplyChainArtifacts | None = None
    trusted: bool = False
    detail: str


def load_artifact_manifest(
    manifest_path: Path,
    probe: ManifestProbe,
    worker_user: str,
) -> ManifestLoadResult:
    """Validate manifest ownership and return fully populated verifier facts."""
    manifest_facts = probe.path_info(str(manifest_path))
    if not _trusted_file(manifest_facts, worker_user):
        return ManifestLoadResult(detail="approved manifest is missing or unsafe")
    manifest_text = probe.file_text(str(manifest_path))
    if manifest_text is None:
        return ManifestLoadResult(detail="approved manifest could not be read")
    try:
        manifest = ArtifactManifest.model_validate(json.loads(manifest_text))
    except (ValueError, TypeError):
        return ManifestLoadResult(detail="approved manifest is invalid")

    evidence_paths = (
        ("SBOM", manifest.sbom_path),
        ("provenance", manifest.provenance_path),
    )
    evidence_text: dict[str, str] = {}
    for label, path in evidence_paths:
        if not Path(path).is_absolute():
            return ManifestLoadResult(detail=f"{label} evidence path is not absolute")
        facts = probe.path_info(path)
        if not _trusted_file(facts, worker_user):
            return ManifestLoadResult(detail=f"{label} evidence is missing or unsafe")
        text = probe.file_text(path)
        if text is None:
            return ManifestLoadResult(detail=f"{label} evidence could not be read")
        evidence_text[label] = text

    signature = manifest.signature
    for path in (signature.public_key_path, signature.certificate_path):
        if path is not None and (
            not Path(path).is_absolute()
            or not _trusted_file(probe.path_info(path), worker_user)
        ):
            return ManifestLoadResult(detail="signature evidence is missing or unsafe")

    artifacts = SupplyChainArtifacts(
        image_digest=manifest.image_digest,
        approved_image_digest=manifest.image_digest,
        sbom_text=evidence_text["SBOM"],
        sbom_image_digest=_find_digest(evidence_text["SBOM"], manifest.image_digest),
        provenance_image_digest=_find_digest(
            evidence_text["provenance"], manifest.image_digest
        ),
        rootfs_digest=None,
        manifest_rootfs_digest=manifest.rootfs_digest,
        signature_command=_signature_command(signature),
    )
    return ManifestLoadResult(
        artifacts=artifacts,
        trusted=True,
        detail="approved manifest and evidence are root-owned and readable",
    )


def _trusted_file(facts: ManifestPathFacts, worker_user: str) -> bool:
    del worker_user
    return (
        facts.exists
        and facts.is_file
        and facts.owner == "root"
        and facts.mode is not None
        and facts.mode & 0o022 == 0
    )


def _signature_command(signature: SignatureManifest) -> tuple[str, ...]:
    command = ["cosign", "verify"]
    if signature.public_key_path:
        command.extend(["--key", signature.public_key_path])
    if signature.certificate_path:
        command.extend(["--certificate", signature.certificate_path])
    if signature.certificate_identity:
        command.extend(["--certificate-identity", signature.certificate_identity])
    if signature.certificate_oidc_issuer:
        command.extend(["--certificate-oidc-issuer", signature.certificate_oidc_issuer])
    command.append(signature.image_reference)
    return tuple(command)


def _find_digest(text: str, expected: str) -> str | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return expected if expected in text else None
    return expected if _contains_value(value, expected) else None


def _contains_value(value: object, expected: str) -> bool:
    if value == expected:
        return True
    if isinstance(value, Mapping):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_value(item, expected) for item in value)
    return False
