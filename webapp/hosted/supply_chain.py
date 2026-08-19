"""Pure verification of the hosted sandbox image supply chain.

The verifier deliberately accepts already-collected artifact facts and an
injected command runner. It never contacts a registry, invokes a shell, or
logs command output.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from collections.abc import Sequence
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
    sbom_attestation_command: tuple[str, ...] = ()
    provenance_attestation_command: tuple[str, ...] = ()
    sbom_attestation_stdout: str | None = None
    provenance_attestation_stdout: str | None = None
    expected_builder_id: str | None = None
    source_repository: str | None = None
    source_commit: str | None = None
    signed_rootfs_digest: str | None = None
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
    sbom_result = _run_command(artifacts.sbom_attestation_command, command_runner)
    provenance_result = _run_command(
        artifacts.provenance_attestation_command, command_runner
    )
    effective = artifacts.model_copy(
        update={
            "sbom_attestation_stdout": (
                sbom_result.stdout
                if sbom_result is not None and sbom_result.returncode == 0
                else None
            ),
            "provenance_attestation_stdout": (
                provenance_result.stdout
                if provenance_result is not None
                and provenance_result.returncode == 0
                else None
            ),
            "signed_rootfs_digest": _extract_signed_rootfs_digest(
                provenance_result.stdout
                if provenance_result is not None
                and provenance_result.returncode == 0
                else ""
            ),
        }
    )
    checks = [
        _check_digest(effective),
        _check_sbom(effective),
        _check_provenance(effective),
        _check_signature(effective, command_runner),
        _check_attestation(
            "sbom_attestation",
            sbom_result,
            effective.image_digest,
            "https://cyclonedx.org/bom",
        ),
        _check_attestation(
            "provenance_attestation",
            provenance_result,
            effective.image_digest,
            "https://slsa.dev/provenance/v1",
        ),
        _check_attested_sbom(effective),
        _check_attested_provenance(effective),
        _check_rootfs(effective),
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
    if not artifacts.image_digest:
        return SupplyChainCheck(check_id="sbom", ok=False, detail="verified image digest is missing")
    if not artifacts.sbom_image_digest:
        return SupplyChainCheck(check_id="sbom", ok=False, detail="SBOM image digest is missing")
    if artifacts.sbom_image_digest and artifacts.sbom_image_digest != artifacts.image_digest:
        return SupplyChainCheck(
            check_id="sbom",
            ok=False,
            detail="SBOM does not cover the verified image digest",
        )
    if artifacts.sbom_attestation_stdout is None:
        return SupplyChainCheck(
            check_id="sbom",
            ok=False,
            detail="verified SBOM attestation output is missing",
        )
    return SupplyChainCheck(check_id="sbom", ok=True, detail="SBOM parses and covers image digest")


def _check_provenance(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    if not artifacts.provenance_image_digest:
        return SupplyChainCheck(check_id="provenance", ok=False, detail="provenance is missing")
    if artifacts.provenance_image_digest != artifacts.image_digest:
        return SupplyChainCheck(
            check_id="provenance",
            ok=False,
            detail="provenance does not match the verified image digest",
        )
    if artifacts.provenance_attestation_stdout is None:
        return SupplyChainCheck(
            check_id="provenance",
            ok=False,
            detail="verified provenance attestation output is missing",
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


def _check_attestation(
    check_id: str,
    result: SupplyChainCommandResult | None,
    image_digest: str | None,
    predicate_type: str,
) -> SupplyChainCheck:
    if result is None:
        return SupplyChainCheck(
            check_id=check_id,
            ok=False,
            detail="attestation verification command is missing",
        )
    if result.returncode == 127:
        return SupplyChainCheck(
            check_id=check_id,
            ok=False,
            detail="attestation verification failed",
        )
    if result.returncode != 0:
        return SupplyChainCheck(
            check_id=check_id,
            ok=False,
            detail="attestation verification failed",
        )
    if not result.stdout:
        return SupplyChainCheck(
            check_id=check_id,
            ok=False,
            detail="attestation verifier output is missing",
        )
    statement = _decode_statement(result.stdout)
    if statement is None or not _statement_matches_image(
        statement, image_digest, predicate_type
    ):
        return SupplyChainCheck(
            check_id=check_id,
            ok=False,
            detail="verified attestation payload is malformed or mismatched",
        )
    return SupplyChainCheck(check_id=check_id, ok=True, detail="attestation verified")


def _run_command(
    command: tuple[str, ...],
    command_runner: SupplyChainCommandRunner,
) -> SupplyChainCommandResult | None:
    if not command:
        return None
    return command_runner.run(command)


def _check_attested_sbom(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    statement = _decode_statement(artifacts.sbom_attestation_stdout or "")
    if statement is None or not isinstance(statement.get("predicate"), dict):
        return SupplyChainCheck(
            check_id="sbom_attestation_binding",
            ok=False,
            detail="verified SBOM predicate is missing",
        )
    if not artifacts.sbom_text:
        return SupplyChainCheck(
            check_id="sbom_attestation_binding",
            ok=False,
            detail="installed SBOM is missing",
        )
    try:
        installed = _canonical_json_hash(json.loads(artifacts.sbom_text))
        signed = _canonical_json_hash(statement["predicate"])
    except (TypeError, ValueError):
        return SupplyChainCheck(
            check_id="sbom_attestation_binding",
            ok=False,
            detail="installed or verified SBOM is invalid",
        )
    return SupplyChainCheck(
        check_id="sbom_attestation_binding",
        ok=installed == signed,
        detail=(
            "verified SBOM matches installed evidence"
            if installed == signed
            else "verified SBOM does not match installed evidence"
        ),
    )


def _check_attested_provenance(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    statement = _decode_statement(artifacts.provenance_attestation_stdout or "")
    if statement is None or not isinstance(statement.get("predicate"), dict):
        return SupplyChainCheck(
            check_id="provenance_attestation_binding",
            ok=False,
            detail="verified provenance predicate is missing",
        )
    predicate = statement["predicate"]
    build_definition = predicate.get("buildDefinition")
    run_details = predicate.get("runDetails")
    dependencies = (
        build_definition.get("resolvedDependencies")
        if isinstance(build_definition, dict)
        else None
    )
    rootfs_digest = _find_rootfs_digest(dependencies)
    builder_id = (
        run_details.get("builder", {}).get("id")
        if isinstance(run_details, dict)
        and isinstance(run_details.get("builder"), dict)
        else None
    )
    source = (
        build_definition.get("externalParameters", {}).get("source")
        if isinstance(build_definition, dict)
        and isinstance(build_definition.get("externalParameters"), dict)
        else None
    )
    source_repository = source.get("uri") if isinstance(source, dict) else None
    source_commit = (
        source.get("digest", {}).get("sha1")
        if isinstance(source, dict) and isinstance(source.get("digest"), dict)
        else None
    )
    checks_ok = (
        rootfs_digest is not None
        and isinstance(build_definition, dict)
        and build_definition.get("buildType")
        == "https://se-skills.dev/sandbox-image"
        and artifacts.expected_builder_id is not None
        and builder_id == artifacts.expected_builder_id
        and artifacts.source_repository is not None
        and source_repository == artifacts.source_repository
        and artifacts.source_commit is not None
        and source_commit == artifacts.source_commit
        and isinstance(source_commit, str)
        and len(source_commit) == 40
    )
    return SupplyChainCheck(
        check_id="provenance_attestation_binding",
        ok=checks_ok,
        detail=(
            "verified provenance contains builder, source, and rootfs binding"
            if checks_ok
            else "verified provenance is missing or mismatched builder, source, or rootfs binding"
        ),
    )


def _extract_signed_rootfs_digest(text: str) -> str | None:
    statement = _decode_statement(text)
    if statement is None or not isinstance(statement.get("predicate"), dict):
        return None
    predicate = statement["predicate"]
    build_definition = predicate.get("buildDefinition")
    dependencies = (
        build_definition.get("resolvedDependencies")
        if isinstance(build_definition, dict)
        else None
    )
    return _find_rootfs_digest(dependencies)


def _check_rootfs(artifacts: SupplyChainArtifacts) -> SupplyChainCheck:
    if not artifacts.rootfs_digest or not artifacts.manifest_rootfs_digest:
        return SupplyChainCheck(check_id="rootfs_digest", ok=False, detail="rootfs digest or manifest is missing")
    if (
        artifacts.rootfs_digest != artifacts.manifest_rootfs_digest
        or artifacts.signed_rootfs_digest != artifacts.rootfs_digest
    ):
        return SupplyChainCheck(
            check_id="rootfs_digest",
            ok=False,
            detail="rootfs digest does not match the approved-image manifest",
        )
    return SupplyChainCheck(check_id="rootfs_digest", ok=True, detail="rootfs matches signed provenance and manifest")


def _decode_statement(text: str) -> dict[str, object] | None:
    try:
        value = json.loads(text)
        envelope = value[0] if isinstance(value, list) and value else value
        if not isinstance(envelope, dict):
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, str):
            return None
        decoded = base64.b64decode(payload, validate=True)
        statement = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return statement if isinstance(statement, dict) else None


def _statement_matches_image(
    statement: dict[str, object],
    image_digest: str | None,
    predicate_type: str,
) -> bool:
    if (
        statement.get("_type") != "https://in-toto.io/Statement/v1"
        or statement.get("predicateType") != predicate_type
        or not image_digest
    ):
        return False
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        return False
    expected = image_digest.removeprefix("sha256:")
    return any(
        isinstance(item, dict)
        and isinstance(item.get("digest"), dict)
        and item["digest"].get("sha256") == expected
        for item in subjects
    )


def _canonical_json_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _find_rootfs_digest(dependencies: object) -> str | None:
    if not isinstance(dependencies, list):
        return None
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        digest = dependency.get("digest")
        if not isinstance(digest, dict):
            continue
        value = digest.get("sha256")
        uri = dependency.get("uri")
        if uri != "urn:se-skills:sandbox-rootfs":
            continue
        if isinstance(value, str) and len(value) == 64:
            return f"sha256:{value}"
    return None
