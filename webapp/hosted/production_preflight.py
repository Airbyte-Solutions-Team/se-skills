"""Production-only preflight assembly and supply-chain evidence loading."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from webapp.hosted import config as hosted_config
from webapp.hosted.preflight import HostProbe, PreflightConfig, PreflightReport, run_preflight
from webapp.hosted.rootfs_digest import digest_rootfs
from webapp.hosted.supply_chain import SupplyChainCheck, verify_supply_chain
from webapp.hosted.supply_chain import SupplyChainCommandResult
from webapp.hosted.supply_chain_manifest import load_artifact_manifest
from webapp.hosted.pins import HostedPins, load_pins


class ProductionPreflightSettings(BaseModel):
    """Redacted settings required to assemble production preflight."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runsc_path: str
    rootfs_path: str
    manifest_path: str
    hosted_env: str
    runtime: str
    worker_user: str
    worker_group: str
    worker_uid: int
    non_worker_uid: int
    database_url: str
    storage_url: str
    anthropic_api_url: str
    model_proxy_secret: str
    present_config_names: frozenset[str]
    approved_https_destinations: frozenset[str] = frozenset()
    sandbox_image_digest: str = ""
    pins: HostedPins = Field(default_factory=load_pins)
    offline: bool = False


class _ProbeCommandRunner:
    """Adapt the host command probe to the pure supply-chain verifier."""

    def __init__(self, probe: HostProbe) -> None:
        self.probe = probe

    def run(self, argv: tuple[str, ...]) -> SupplyChainCommandResult:
        return self.probe.command(argv)


def run_production_preflight(
    settings: ProductionPreflightSettings,
    probe: HostProbe,
    rootfs_digest_fn: Callable[[Path], str] = digest_rootfs,
) -> PreflightReport:
    """Load trusted evidence, hash the deployed rootfs, and run host checks."""
    manifest = (
        load_artifact_manifest(
            Path(settings.manifest_path),
            probe,
        )
        if not settings.offline
        else None
    )
    supply_chain = None
    if manifest is not None and manifest.trusted and manifest.artifacts is not None:
        rootfs_digest = ""
        rootfs_path = Path(settings.rootfs_path)
        if rootfs_path.is_dir():
            try:
                rootfs_digest = rootfs_digest_fn(rootfs_path)
            except OSError:
                rootfs_digest = ""
        artifacts = manifest.artifacts.model_copy(
            update={
                "image_digest": settings.sandbox_image_digest or None,
                "rootfs_digest": rootfs_digest,
            }
        )
        supply_chain = verify_supply_chain(artifacts, _ProbeCommandRunner(probe))
        approved_digest = manifest.artifacts.approved_image_digest
        configured_digest_ok = bool(settings.sandbox_image_digest) and (
            settings.sandbox_image_digest == approved_digest
        )
        pinned_digest_ok = (
            settings.pins.sandbox_image.approved_digest is None
            or settings.pins.sandbox_image.approved_digest == approved_digest
        )
        digest_check = SupplyChainCheck(
            check_id="image_digest_configuration",
            ok=configured_digest_ok and pinned_digest_ok,
            detail=(
                "configured image digest matches approved evidence"
                if configured_digest_ok and pinned_digest_ok
                else "configured image digest does not match approved evidence"
            ),
        )
        supply_chain = supply_chain.model_copy(
            update={"checks": (*supply_chain.checks, digest_check)}
        )
    report = run_preflight(
        PreflightConfig(
            runsc_path=settings.runsc_path,
            present_config_names=settings.present_config_names,
            model_proxy_secret=settings.model_proxy_secret,
            anthropic_api_url=settings.anthropic_api_url,
            database_url=settings.database_url,
            storage_url=settings.storage_url,
            rootfs_path=settings.rootfs_path,
            runsc_state_path=hosted_config.RUNSC_STATE_DIR,
            bundle_path=hosted_config.RUNSC_BUNDLE_DIR,
            worker_user=settings.worker_user,
            worker_group=settings.worker_group,
            worker_uid=settings.worker_uid,
            non_worker_uid=settings.non_worker_uid,
            pins=settings.pins,
            supply_chain=supply_chain,
            supply_chain_manifest_status=(
                None if manifest is None else ("ok" if manifest.trusted else "fail")
            ),
            supply_chain_manifest_detail=(
                "" if manifest is None else manifest.detail
            ),
            supply_chain_skipped=settings.offline,
            hosted_env=settings.hosted_env,
            runtime=settings.runtime,
            approved_https_destinations=settings.approved_https_destinations,
        ),
        probe,
    )
    return report
