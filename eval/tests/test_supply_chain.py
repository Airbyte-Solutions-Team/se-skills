from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from webapp.hosted.supply_chain import (
    SupplyChainArtifacts,
    SupplyChainCommandResult,
    SupplyChainVerification,
    verify_supply_chain,
)


class _CommandRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[Sequence[str]] = []

    def run(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        self.calls.append(argv)
        return SupplyChainCommandResult(returncode=self.returncode)


def _artifacts() -> SupplyChainArtifacts:
    digest = "sha256:" + "a" * 64
    rootfs = "sha256:" + "b" * 64
    return SupplyChainArtifacts(
        image_digest=digest,
        approved_image_digest=digest,
        sbom_text=json.dumps({"bomFormat": "CycloneDX"}),
        sbom_image_digest=digest,
        provenance_image_digest=digest,
        rootfs_digest=rootfs,
        manifest_rootfs_digest=rootfs,
        signature_command=("cosign", "verify"),
    )


def test_supply_chain_accepts_matching_artifacts() -> None:
    runner = _CommandRunner()
    result = verify_supply_chain(_artifacts(), runner)

    assert result.ok
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("field", "value", "check_id"),
    [
        ("image_digest", "sha256:" + "c" * 64, "image_digest"),
        ("sbom_image_digest", "sha256:" + "c" * 64, "sbom"),
        ("provenance_image_digest", "sha256:" + "c" * 64, "provenance"),
        ("rootfs_digest", "sha256:" + "c" * 64, "rootfs_digest"),
    ],
)
def test_supply_chain_rejects_digest_mismatch(
    field: str, value: str, check_id: str
) -> None:
    result = verify_supply_chain(
        _artifacts().model_copy(update={field: value}),
        _CommandRunner(),
    )

    check = next(item for item in result.checks if item.check_id == check_id)
    assert not check.ok
    assert value not in check.detail


def test_supply_chain_rejects_missing_or_invalid_sbom() -> None:
    missing = verify_supply_chain(
        _artifacts().model_copy(update={"sbom_text": None}),
        _CommandRunner(),
    )
    invalid = verify_supply_chain(
        _artifacts().model_copy(update={"sbom_text": "not-json"}),
        _CommandRunner(),
    )

    assert not next(item for item in missing.checks if item.check_id == "sbom").ok
    assert not next(item for item in invalid.checks if item.check_id == "sbom").ok


def test_supply_chain_honors_signature_failure() -> None:
    result = verify_supply_chain(_artifacts(), _CommandRunner(returncode=1))

    signature = next(item for item in result.checks if item.check_id == "signature")
    assert not signature.ok
    assert "cosign" not in signature.detail


def test_pins_match_dockerfile_base_digests() -> None:
    pins = json.loads(Path("deploy/pins.json").read_text(encoding="utf-8"))
    dockerfile = Path("webapp/hosted/runsc/Dockerfile").read_text(encoding="utf-8")

    assert pins["sandbox_image"]["base_digests"]["python"] in dockerfile
    assert pins["sandbox_image"]["base_digests"]["distroless"] in dockerfile
