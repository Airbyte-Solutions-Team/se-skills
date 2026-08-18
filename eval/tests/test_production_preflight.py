from __future__ import annotations

import json
import base64
import os
from pathlib import Path
from typing import Sequence

import pytest

from webapp.hosted.preflight import ListeningSocket, PathFacts
from webapp.hosted.production_preflight import (
    ProductionPreflightSettings,
    run_production_preflight,
)
from webapp.hosted.supply_chain import SupplyChainCommandResult
from scripts.install_sandbox_evidence import install_evidence


IMAGE_DIGEST = "sha256:" + "a" * 64
ROOTFS_DIGEST = "sha256:" + "b" * 64


class _Probe:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.signature_returncode = 0
        self.unsafe: set[str] = set()
        self.firewall = """table inet se_skills {
  chain input {
    type filter hook input priority 0; policy drop;
    iifname "lo" accept
    ct state established,related accept
  }
  chain output {
    type filter hook output priority 0; policy drop;
    oifname "lo" accept
    ct state established,related accept
    ip daddr 169.254.0.0/16 drop
    ip6 daddr fe80::/10 drop
    ip6 daddr fd00::/8 drop
    meta skuid 995 ip daddr 203.0.113.10 tcp dport 443 accept
    meta skuid 995 ip daddr 203.0.113.10 tcp dport 3128 accept
  }
}"""

    def os_release(self) -> tuple[str, str]:
        return "ubuntu", "24.04"

    def kernel_version(self) -> str:
        return "6.8.0"

    def architecture(self) -> str:
        return "x86_64"

    def path_info(self, path: str) -> PathFacts:
        candidate = Path(path)
        if path in self.unsafe:
            return PathFacts(True, "se-worker", "se-worker", 0o775, True, False)
        if path == "/usr/local/bin/runsc":
            return PathFacts(
                True, "root", "root", 0o755, True, False,
                "4463ce276e207f5a516a08ec627a768a19cf7bed0094d522b0810bee3424585caa8d344e093204012b974f5c508ab2362dcb0d7236f0c1992fccc426beeb7ffc",
            )
        if path == "/var/lib/se-skills":
            return PathFacts(True, "se-worker", "se-worker", 0o750, False, True)
        if path in {"/var/lib/se-skills/runsc", "/var/lib/se-skills/bundles"}:
            return PathFacts(True, "se-worker", "se-worker", 0o700, False, True)
        if path == str(self.root):
            return PathFacts(True, "root", "root", 0o755, False, True)
        if candidate.is_file():
            return PathFacts(True, "root", "root", 0o644, True, False)
        if candidate.is_dir():
            return PathFacts(True, "root", "root", 0o755, False, True)
        return PathFacts(False, None, None, None, False, False)

    def command(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        if argv[:2] == ("nft", "list"):
            return SupplyChainCommandResult(returncode=0, stdout=self.firewall)
        if argv and argv[0] == "cosign":
            sbom = "cyclonedx" in argv
            predicate = (
                {
                    "metadata": {
                        "component": {
                            "hashes": [
                                {"alg": "SHA-256", "content": "a" * 64}
                            ]
                        }
                    }
                }
                if sbom
                else {
                    "buildDefinition": {
                        "buildType": "https://se-skills.dev/sandbox-image",
                        "externalParameters": {
                            "source": {
                                "uri": "https://github.com/example/repo",
                                "digest": {"sha1": "c" * 40},
                            }
                        },
                        "resolvedDependencies": [
                            {
                                "uri": "urn:se-skills:sandbox-rootfs",
                                "digest": {"sha256": "b" * 64},
                            }
                        ]
                    },
                    "runDetails": {
                        "builder": {
                            "id": "https://github.com/example/repo/.github/workflows/"
                            "sandbox-image-release.yml@refs/heads/main"
                        },
                    },
                }
            )
            statement = {
                "_type": "https://in-toto.io/Statement/v1",
                "predicateType": (
                    "https://cyclonedx.org/bom"
                    if sbom
                    else "https://slsa.dev/provenance/v1"
                ),
                "subject": [{"digest": {"sha256": "a" * 64}}],
                "predicate": predicate,
            }
            return SupplyChainCommandResult(
                returncode=self.signature_returncode,
                stdout=json.dumps(
                    {
                        "payload": base64.b64encode(
                            json.dumps(statement).encode()
                        ).decode()
                    }
                ),
            )
        return SupplyChainCommandResult(returncode=0, stdout="runsc 20260810.0")

    def cgroup_version(self) -> int | None:
        return 2

    def namespace_features(self) -> frozenset[str]:
        return frozenset({"pid", "net", "user"})

    def listening_sockets(self) -> tuple[ListeningSocket, ...]:
        return (ListeningSocket("127.0.0.1", 0),)

    def clock_offset_seconds(self) -> float:
        return 0.1

    def disk_free_bytes(self, path: str) -> int:
        return 20_000_000_000

    def file_text(self, path: str) -> str | None:
        candidate = Path(path)
        return candidate.read_text(encoding="utf-8") if candidate.is_file() else None


def _setup(tmp_path: Path) -> tuple[_Probe, ProductionPreflightSettings, dict[str, Path]]:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "app").write_text("safe", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    sbom_path = tmp_path / "sbom.json"
    provenance_path = tmp_path / "provenance.json"
    sbom_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "component": {
                        "hashes": [
                            {
                                "alg": "SHA-256",
                                "content": IMAGE_DIGEST.removeprefix("sha256:"),
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    provenance_path.write_text(
        json.dumps(
            {
                "_type": "https://in-toto.io/Statement/v1",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [
                    {
                        "digest": {
                            "sha256": IMAGE_DIGEST.removeprefix("sha256:")
                        }
                    }
                ],
                "predicate": {
                    "buildDefinition": {
                        "resolvedDependencies": [
                            {
                                "uri": "urn:se-skills:sandbox-rootfs",
                                "digest": {"sha256": ROOTFS_DIGEST.removeprefix("sha256:")},
                            }
                        ]
                    },
                    "runDetails": {
                        "builder": {"id": "release@example.invalid"}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "image_digest": IMAGE_DIGEST,
                "rootfs_digest": ROOTFS_DIGEST,
                "sbom_path": str(sbom_path),
                "provenance_path": str(provenance_path),
                "source_repository": "https://github.com/example/repo",
                "source_commit": "c" * 40,
                "signature": {
                    "image_reference": f"registry.example/image@{IMAGE_DIGEST}",
                    "certificate_identity": (
                        "https://github.com/example/repo/.github/workflows/"
                        "sandbox-image-release.yml@refs/heads/main"
                    ),
                    "certificate_oidc_issuer": "https://issuer.example",
                },
            }
        ),
        encoding="utf-8",
    )
    probe = _Probe(rootfs)
    settings = ProductionPreflightSettings(
        runsc_path="/usr/local/bin/runsc",
        rootfs_path=str(rootfs),
        manifest_path=str(manifest_path),
        hosted_env="production",
        runtime="post-call-runsc",
        worker_user="se-worker",
        worker_group="se-worker",
        worker_uid=995,
        non_worker_uid=994,
        database_url="postgresql://worker@db/app?sslmode=require",
        storage_url="https://storage.example",
        anthropic_api_url="https://api.anthropic.com",
        anthropic_proxy_url="http://203.0.113.10:3128",
        anthropic_proxy_host="203.0.113.10",
        anthropic_proxy_port=3128,
        model_proxy_secret="PreflightSecretWithSufficientDiversity0123",
        present_config_names=frozenset(
            {
                "DATABASE_WORKER_URL",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_EGRESS_PROXY_URL",
                "MODEL_PROXY_SECRET",
                "RUNSC_ROOTFS",
                "SANDBOX_IMAGE_DIGEST",
                "SANDBOX_MANIFEST_PATH",
            }
        ),
        sandbox_image_digest=IMAGE_DIGEST,
        approved_https_destinations=frozenset({"203.0.113.10"}),
    )
    return probe, settings, {
        "manifest": manifest_path,
        "sbom": sbom_path,
        "provenance": provenance_path,
        "rootfs": rootfs,
    }


def _report(tmp_path: Path):
    probe, settings, paths = _setup(tmp_path)
    report = run_production_preflight(
        settings,
        probe,
        rootfs_digest_fn=lambda path: ROOTFS_DIGEST,
    )
    return report, probe, settings, paths


def test_production_preflight_valid_configuration_passes(tmp_path: Path) -> None:
    report, _, _, _ = _report(tmp_path)

    assert report.ok


def test_build_manifest_promotion_round_trip_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True)
    digest_hex = IMAGE_DIGEST.removeprefix("sha256:")
    (bundle / f"{digest_hex}.sbom.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "component": {
                        "hashes": [{"alg": "SHA-256", "content": digest_hex}]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (bundle / f"{digest_hex}.provenance.json").write_text(
        json.dumps(
            {
                "_type": "https://in-toto.io/Statement/v1",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subject": [{"digest": {"sha256": digest_hex}}],
            }
        ),
        encoding="utf-8",
    )
    build_manifest = bundle / "build.manifest.json"
    build_manifest.write_text(
        json.dumps(
            {
                "image_digest": IMAGE_DIGEST,
                "rootfs_digest": ROOTFS_DIGEST,
                "sbom_name": f"{digest_hex}.sbom.json",
                    "provenance_name": f"{digest_hex}.provenance.json",
                    "source_repository": "https://github.com/example/repo",
                    "source_commit": "c" * 40,
                "signature": {
                    "image_reference": f"registry.example/image@{IMAGE_DIGEST}",
                    "certificate_identity": "https://github.com/example/repo/.github/workflows/sandbox-image-release.yml@refs/heads/main",
                    "certificate_oidc_issuer": "https://token.actions.githubusercontent.com",
                },
            }
        ),
        encoding="utf-8",
    )
    installed = tmp_path / "etc/se-skills/sandbox-manifest.json"
    install_evidence(
        bundle,
        build_manifest,
        tmp_path / "etc/se-skills/evidence",
        installed,
        geteuid_fn=lambda: 0,
        owner_uid_fn=lambda path: 0,
    )
    evidence_dir = tmp_path / "etc/se-skills/evidence" / digest_hex
    assert evidence_dir.stat().st_mode & 0o022 == 0
    assert evidence_dir.parent.stat().st_mode & 0o022 == 0
    assert installed.parent.stat().st_mode & 0o022 == 0
    probe, settings, paths = _setup(tmp_path)
    settings = settings.model_copy(
        update={
            "manifest_path": str(installed),
            "rootfs_path": str(paths["rootfs"]),
        }
    )
    report = run_production_preflight(
        settings, probe, rootfs_digest_fn=lambda path: ROOTFS_DIGEST
    )
    assert report.ok


def _installer_fixture(
    tmp_path: Path, image_digest: str = IMAGE_DIGEST
) -> tuple[Path, Path, Path]:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True)
    sbom_name = "evidence.sbom.json"
    provenance_name = "evidence.provenance.json"
    (bundle / sbom_name).write_text("{}", encoding="utf-8")
    (bundle / provenance_name).write_text("{}", encoding="utf-8")
    manifest = bundle / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "image_digest": image_digest,
                "rootfs_digest": ROOTFS_DIGEST,
                "sbom_name": sbom_name,
                "provenance_name": provenance_name,
                "source_repository": "https://github.com/example/repo",
                "source_commit": "c" * 40,
                "signature": {
                    "image_reference": f"registry.example/image@{image_digest}",
                    "certificate_identity": "builder",
                    "certificate_oidc_issuer": "issuer",
                },
            }
        ),
        encoding="utf-8",
    )
    return bundle, manifest, tmp_path / "etc/se-skills/sandbox-manifest.json"


def test_evidence_installer_rejects_traversal_and_unsafe_names(tmp_path: Path) -> None:
    bundle, manifest, output = _installer_fixture(tmp_path)
    data = json.loads(manifest.read_text())
    data["image_digest"] = "sha256:../../etc"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="sha256 image digest"):
        install_evidence(
            bundle,
            manifest,
            tmp_path / "etc/se-skills/evidence",
            output,
            geteuid_fn=lambda: 0,
            owner_uid_fn=lambda path: 0,
        )

    bundle, manifest, output = _installer_fixture(tmp_path / "names")
    data = json.loads(manifest.read_text())
    data["sbom_name"] = "../escape"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsafe"):
        install_evidence(
            bundle,
            manifest,
            tmp_path / "names/etc/se-skills/evidence",
            output,
            geteuid_fn=lambda: 0,
            owner_uid_fn=lambda path: 0,
        )


@pytest.mark.parametrize("source_kind", ("symlink", "fifo", "oversized"))
def test_evidence_installer_rejects_unsafe_sources(
    tmp_path: Path, source_kind: str
) -> None:
    bundle, manifest, output = _installer_fixture(tmp_path)
    source = bundle / "evidence.sbom.json"
    if source_kind == "symlink":
        source.unlink()
        source.symlink_to("/etc/hosts")
    elif source_kind == "fifo":
        source.unlink()
        os.mkfifo(source)
    else:
        with source.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        install_evidence(
            bundle,
            manifest,
            tmp_path / "etc/se-skills/evidence",
            output,
            geteuid_fn=lambda: 0,
            owner_uid_fn=lambda path: 0,
        )


def test_evidence_installer_refuses_different_replacement_and_accepts_idempotent(
    tmp_path: Path,
) -> None:
    bundle, manifest, output = _installer_fixture(tmp_path)
    root = tmp_path / "etc/se-skills/evidence"
    install_evidence(
        bundle,
        manifest,
        root,
        output,
        geteuid_fn=lambda: 0,
        owner_uid_fn=lambda path: 0,
    )
    source = bundle / "evidence.sbom.json"
    source.write_text("different", encoding="utf-8")
    with pytest.raises(FileExistsError, match="rotate or remove"):
        install_evidence(
            bundle,
            manifest,
            root,
            output,
            geteuid_fn=lambda: 0,
            owner_uid_fn=lambda path: 0,
        )
    source.write_text("{}", encoding="utf-8")
    install_evidence(
        bundle,
        manifest,
        root,
        output,
        geteuid_fn=lambda: 0,
        owner_uid_fn=lambda path: 0,
    )


def test_evidence_installer_requires_root(tmp_path: Path) -> None:
    bundle, manifest, output = _installer_fixture(tmp_path)
    with pytest.raises(PermissionError, match="must run as root"):
        install_evidence(
            bundle,
            manifest,
            tmp_path / "etc/se-skills/evidence",
            output,
            geteuid_fn=lambda: 1000,
            owner_uid_fn=lambda path: 0,
        )


@pytest.mark.parametrize("case", ("manifest", "manifest_dir", "sbom"))
def test_production_preflight_trust_failures_fail_closed(
    tmp_path: Path, case: str
) -> None:
    _, probe, settings, paths = _report(tmp_path)
    if case == "manifest":
        paths["manifest"].unlink()
    elif case == "manifest_dir":
        probe.unsafe.add(str(paths["manifest"].parent))
    else:
        probe.unsafe.add(str(paths["sbom"]))

    report = run_production_preflight(
        settings,
        probe,
        rootfs_digest_fn=lambda path: ROOTFS_DIGEST,
    )

    assert not report.ok
    assert any(check.check_id == "supply_chain_manifest" for check in report.failed_required())


@pytest.mark.parametrize(
    "case",
    (
        "unpublished",
        "unparseable_sbom",
        "sbom_digest",
        "missing_provenance",
        "provenance_digest",
        "signature",
        "configured_digest",
        "rootfs",
    ),
)
def test_production_preflight_evidence_failures_fail_closed(
    tmp_path: Path, case: str
) -> None:
    _, settings, paths = _setup(tmp_path)
    probe = _Probe(paths["rootfs"])
    if case == "unpublished":
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        manifest["image_digest"] = "unpublished"
        paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "unparseable_sbom":
        paths["sbom"].write_text("{", encoding="utf-8")
    elif case == "sbom_digest":
        paths["sbom"].write_text(json.dumps({"subject": []}), encoding="utf-8")
    elif case == "missing_provenance":
        paths["provenance"].unlink()
    elif case == "provenance_digest":
        paths["provenance"].write_text(
            json.dumps({"subject": [{"digest": "sha256:" + "c" * 64}]}),
            encoding="utf-8",
        )
    elif case == "signature":
        probe.signature_returncode = 127
    elif case == "configured_digest":
        settings = settings.model_copy(update={"sandbox_image_digest": "sha256:" + "c" * 64})
    else:
        rootfs_digest = lambda path: "sha256:" + "c" * 64
        report = run_production_preflight(settings, probe, rootfs_digest_fn=rootfs_digest)
        assert not report.ok
        return

    report = run_production_preflight(
        settings,
        probe,
        rootfs_digest_fn=lambda path: ROOTFS_DIGEST,
    )
    assert not report.ok
