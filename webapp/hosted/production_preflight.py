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
    runsc_helper_path: str = "/usr/local/sbin/se-skills-runsc"
    runsc_sudoers_path: str = "/etc/sudoers.d/se-skills-runsc"
    runsc_broker_config_path: str = "/etc/se-skills/runsc-broker.json"
    broker_interpreter_path: str = "/usr/bin/python3"
    broker_script_path: str = "/usr/local/sbin/se-skills-runsc"
    broker_venv_path: str = ""
    broker_import_paths: tuple[str, ...] = ()
    rootfs_path: str
    manifest_path: str
    hosted_env: str
    runtime: str
    worker_user: str
    worker_group: str
    worker_uid: int
    non_worker_uid: int
    sandbox_uid: int = 65532
    sandbox_gid: int = 65532
    journal_phase_pause_seconds: float = 0.0
    database_url: str
    storage_url: str
    anthropic_api_url: str
    model_proxy_secret: str
    present_config_names: frozenset[str]
    approved_https_destinations: frozenset[str] = frozenset()
    sandbox_image_digest: str = ""
    pins: HostedPins = Field(default_factory=load_pins)
    offline: bool = False
    anthropic_proxy_url: str = ""
    anthropic_proxy_host: str = ""
    anthropic_proxy_port: int = 3128
    approved_management_ssh_cidr: str = ""
    approved_management_ssh_port: int = 22


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
            runsc_helper_path=settings.runsc_helper_path,
            runsc_sudoers_path=settings.runsc_sudoers_path,
            runsc_broker_config_path=settings.runsc_broker_config_path,
            broker_interpreter_path=settings.broker_interpreter_path,
            broker_script_path=settings.broker_script_path,
            broker_venv_path=settings.broker_venv_path,
            broker_import_paths=settings.broker_import_paths,
            present_config_names=settings.present_config_names,
            model_proxy_secret=settings.model_proxy_secret,
            anthropic_api_url=settings.anthropic_api_url,
            anthropic_proxy_url=settings.anthropic_proxy_url,
            anthropic_proxy_host=settings.anthropic_proxy_host,
            anthropic_proxy_port=settings.anthropic_proxy_port,
            database_url=settings.database_url,
            storage_url=settings.storage_url,
            rootfs_path=settings.rootfs_path,
            runsc_state_path=hosted_config.RUNSC_STATE_DIR,
            bundle_path=hosted_config.RUNSC_BUNDLE_DIR,
            workspace_path=hosted_config.RUNSC_WORKSPACE_ROOT,
            worker_user=settings.worker_user,
            worker_group=settings.worker_group,
            worker_uid=settings.worker_uid,
            non_worker_uid=settings.non_worker_uid,
            sandbox_uid=settings.sandbox_uid,
            sandbox_gid=settings.sandbox_gid,
            journal_phase_pause_seconds=settings.journal_phase_pause_seconds,
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
            approved_management_ssh_cidr=settings.approved_management_ssh_cidr,
            approved_management_ssh_port=settings.approved_management_ssh_port,
        ),
        probe,
    )
    return report
