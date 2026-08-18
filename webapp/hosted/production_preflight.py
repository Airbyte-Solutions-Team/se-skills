"""Production-only preflight assembly and supply-chain evidence loading."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from webapp.hosted import config as hosted_config
from webapp.hosted.preflight import HostProbe, PreflightConfig, PreflightReport, run_preflight
from webapp.hosted.rootfs_digest import digest_rootfs
from webapp.hosted.supply_chain import SupplyChainArtifacts, verify_supply_chain
from webapp.hosted.supply_chain_manifest import load_artifact_manifest


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
    database_url: str
    storage_url: str
    anthropic_api_url: str
    model_proxy_secret: str
    present_config_names: frozenset[str]
    approved_https_destinations: frozenset[str] = frozenset()
    offline: bool = False


def run_production_preflight(
    settings: ProductionPreflightSettings,
    probe: HostProbe,
) -> PreflightReport:
    """Load trusted evidence, hash the deployed rootfs, and run host checks."""
    manifest = (
        load_artifact_manifest(
            Path(settings.manifest_path),
            probe,
            settings.worker_user,
        )
        if not settings.offline
        else None
    )
    supply_chain = None
    if manifest is not None and manifest.trusted and manifest.artifacts is not None:
        rootfs_digest = _rootfs_digest(settings.rootfs_path)
        artifacts = manifest.artifacts.model_copy(update={"rootfs_digest": rootfs_digest})
        supply_chain = verify_supply_chain(artifacts, probe)
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


def _rootfs_digest(path: str) -> str | None:
    rootfs = Path(path)
    if not rootfs.is_dir():
        return None
    try:
        return digest_rootfs(rootfs)
    except OSError:
        return None
