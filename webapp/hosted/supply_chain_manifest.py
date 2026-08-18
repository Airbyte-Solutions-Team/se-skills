"""Load trusted, host-installed sandbox supply-chain evidence."""
from __future__ import annotations

import json
import re
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
    is_directory: bool


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
    sbom_path: str | None = None
    provenance_path: str | None = None
    sbom_name: str | None = None
    provenance_name: str | None = None
    signature: SignatureManifest

    @property
    def published(self) -> bool:
        return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest))


class ManifestLoadResult(BaseModel):
    """Redacted result of loading and trusting an artifact manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifacts: SupplyChainArtifacts | None = None
    trusted: bool = False
    detail: str


def load_artifact_manifest(
    manifest_path: Path,
    probe: ManifestProbe,
) -> ManifestLoadResult:
    """Validate manifest ownership and return fully populated verifier facts."""
    manifest_facts = probe.path_info(str(manifest_path))
    if not _trusted_file(
        manifest_facts, probe.path_info(str(manifest_path.parent))
    ):
        return ManifestLoadResult(detail="approved manifest is missing or unsafe")
    manifest_text = probe.file_text(str(manifest_path))
    if manifest_text is None:
        return ManifestLoadResult(detail="approved manifest could not be read")
    try:
        manifest = ArtifactManifest.model_validate(json.loads(manifest_text))
    except (ValueError, TypeError):
        return ManifestLoadResult(detail="approved manifest is invalid")
    if not manifest.published:
        return ManifestLoadResult(detail="approved manifest is unpublished")

    evidence_paths = (
        ("SBOM", manifest.sbom_path),
        ("provenance", manifest.provenance_path),
    )
    evidence_text: dict[str, str] = {}
    for label, path in evidence_paths:
        if path is None:
            return ManifestLoadResult(detail=f"{label} evidence path is missing")
        if not Path(path).is_absolute():
            return ManifestLoadResult(detail=f"{label} evidence path is not absolute")
        facts = probe.path_info(path)
        if not _trusted_file(facts, probe.path_info(str(Path(path).parent))):
            return ManifestLoadResult(detail=f"{label} evidence is missing or unsafe")
        text = probe.file_text(path)
        if text is None:
            return ManifestLoadResult(detail=f"{label} evidence could not be read")
        evidence_text[label] = text

    signature = manifest.signature
    if (
        not signature.public_key_path
        and (not signature.certificate_identity or not signature.certificate_oidc_issuer)
    ):
        return ManifestLoadResult(detail="keyless signature constraints are missing")
    for path in (signature.public_key_path, signature.certificate_path):
        if path is not None and (
            not Path(path).is_absolute()
            or not _trusted_file(
                probe.path_info(path), probe.path_info(str(Path(path).parent))
            )
        ):
            return ManifestLoadResult(detail="signature evidence is missing or unsafe")

    artifacts = SupplyChainArtifacts(
        image_digest=None,
        approved_image_digest=manifest.image_digest,
        sbom_text=evidence_text["SBOM"],
        sbom_image_digest=_find_sbom_digest(evidence_text["SBOM"]),
        provenance_image_digest=_find_provenance_digest(evidence_text["provenance"]),
        rootfs_digest=None,
        manifest_rootfs_digest=manifest.rootfs_digest,
        signature_command=_signature_command(signature),
        sbom_attestation_command=_attestation_command(signature, "cyclonedx"),
        provenance_attestation_command=_attestation_command(
            signature, "slsaprovenance"
        ),
    )
    return ManifestLoadResult(
        artifacts=artifacts,
        trusted=True,
        detail="approved manifest and evidence are root-owned and readable",
    )


def _trusted_file(
    facts: ManifestPathFacts,
    parent_facts: ManifestPathFacts,
) -> bool:
    return (
        facts.exists
        and facts.is_file
        and facts.owner == "root"
        and facts.mode is not None
        and facts.mode & 0o022 == 0
        and parent_facts.exists
        and parent_facts.is_directory
        and parent_facts.owner == "root"
        and parent_facts.mode is not None
        and parent_facts.mode & 0o022 == 0
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


def _attestation_command(
    signature: SignatureManifest, predicate_type: str
) -> tuple[str, ...]:
    command = ["cosign", "verify-attestation", "--type", predicate_type]
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


def _find_sbom_digest(text: str) -> str | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    metadata = value.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        return None
    hashes = component.get("hashes")
    if isinstance(hashes, list):
        for item in hashes:
            if isinstance(item, dict) and item.get("alg") == "SHA-256":
                digest = item.get("content")
                if (
                    isinstance(digest, str)
                    and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                ):
                    return f"sha256:{digest.lower()}"
    for field in ("version", "purl", "bom-ref"):
        value = component.get(field)
        if not isinstance(value, str):
            continue
        match = re.search(r"(?:sha256:)?([0-9a-fA-F]{64})", value)
        if match:
            return f"sha256:{match.group(1).lower()}"
    return None


def _find_provenance_digest(text: str) -> str | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("_type") != "https://in-toto.io/Statement/v1":
        return None
    if value.get("predicateType") != "https://slsa.dev/provenance/v1":
        return None
    subjects = value.get("subject")
    if not isinstance(subjects, list):
        return None
    for subject in subjects:
        if not isinstance(subject, dict):
            continue
        digest = subject.get("digest")
        if isinstance(digest, dict) and isinstance(digest.get("sha256"), str):
            value = digest["sha256"]
            if len(value) == 64:
                return f"sha256:{value}"
    return None
