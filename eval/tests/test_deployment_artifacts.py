from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "deploy" / "ansible"


def test_ansible_package_has_expected_entrypoints_and_role_tasks() -> None:
    assert (ANSIBLE / "site.yml").exists()
    assert (ANSIBLE / "uninstall.yml").exists()
    for name in (
        "preflight.yml",
        "user.yml",
        "directories.yml",
        "runsc.yml",
        "systemd.yml",
        "firewall.yml",
        "logging.yml",
        "cleanup.yml",
    ):
        parsed = yaml.safe_load(
            (ANSIBLE / "roles" / "hosted_worker" / "tasks" / name).read_text()
        )
        assert isinstance(parsed, list)


def test_ansible_package_has_check_mode_safe_pinned_runsc_and_hardening() -> None:
    runsc = (ANSIBLE / "roles" / "hosted_worker" / "tasks" / "runsc.yml").read_text()
    service = (
        ANSIBLE
        / "roles"
        / "hosted_worker"
        / "templates"
        / "se-skills-worker.service.j2"
    ).read_text()
    all_text = "\n".join(
        path.read_text() for path in ANSIBLE.rglob("*") if path.is_file()
    )

    assert 'checksum: "sha512:' in runsc
    assert "when: not ansible_check_mode" in runsc
    for directive in (
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "MemoryMax=",
        "CPUQuota=",
        "TasksMax=",
        "LimitNOFILE=",
        "LimitFSIZE=",
    ):
        assert directive in service
    assert "curl | sh" not in all_text
    firewall = (
        ANSIBLE / "roles" / "hosted_worker" / "templates" / "firewall.nft.j2"
    ).read_text()
    assert "policy drop" in firewall
    assert "169.254.169.254" in firewall
    assert "fd00:ec2::254" in firewall
    assert "flush ruleset" not in firewall
    assert "meta skuid" in firewall
    assert "\n    tcp dport 443 accept" not in firewall
    assert "ghcr.io" not in firewall


def test_worker_requirements_match_standalone_metadata() -> None:
    requirements = {
        line.split("==", 1)[0].lower()
        for line in (ROOT / "deploy/requirements-worker.txt").read_text().splitlines()
        if line.strip()
    }
    script = (ROOT / "scripts/run_hosted_worker.py").read_text()

    assert requirements == {
        "asyncpg",
        "fastapi",
        "httpx",
        "pydantic",
        "pyjwt[crypto]",
        "uvicorn[standard]",
    }
    for version in ("0.30.0", "2.10.0", "2.0", "0.25", "0.100", "0.30.0"):
        assert version in script
    service = (
        ANSIBLE
        / "roles"
        / "hosted_worker"
        / "templates"
        / "se-skills-worker.service.j2"
    ).read_text()
    assert "Environment=HOSTED_MODE=1" in service
    assert "Environment=HOSTED_ENV=production" in service
    assert "hosted_venv_dir" in service
    defaults = (
        ANSIBLE / "roles" / "hosted_worker" / "defaults" / "main.yml"
    ).read_text()
    assert "hosted_dns_servers: []" in defaults
    assert "hosted_registry_host: \"\"" in defaults


def test_uninstall_does_not_touch_durable_services_or_data() -> None:
    text = (ANSIBLE / "uninstall.yml").read_text()

    assert "postgres" not in text.lower()
    assert "storage" not in text.lower()
    assert "/opt/se-skills" in text
    assert "/var/lib/se-skills" in text
    for path in (
        "/usr/local/sbin/se-skills-cleanup-stale-sandboxes",
        "/usr/local/bin/runsc",
        "/etc/se-skills",
        "/opt/se-skills/venv",
        "/etc/systemd/journald.conf.d/se-skills-worker.conf",
        "/etc/systemd/system/se-skills-cleanup.service",
        "/etc/systemd/system/se-skills-cleanup.timer",
    ):
        assert path in text


def test_image_build_script_has_fail_closed_supply_chain_guardrails() -> None:
    script = (ROOT / "deploy/images/build_sandbox_image.sh").read_text()

    assert "set -euo pipefail" in script
    assert "require_tool syft" in script
    assert "required vulnerability scanner" in script
    assert "cosign sign" in script
    assert "docker export" in script
    assert "rootfs_digest" in script
    assert "sbom" in script
    assert "docker build --pull=false" in script
    assert "image_config_id" in script
    assert "RepoDigests" in script
    assert script.index("docker push") < script.index("cosign sign")
    assert "--same-owner" in script
    assert "curl | sh" not in script


def test_release_workflow_is_manual_and_confirmed() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/sandbox-image-release.yml").read_text()
    )
    text = (ROOT / ".github/workflows/sandbox-image-release.yml").read_text()

    assert "workflow_dispatch" in text
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "BUILD_SANDBOX_IMAGE" in text
    assert "environment: sandbox-release" in text
    assert "PUBLISH: \"1\"" in text
    assert "docker push" in (
        ROOT / "deploy/images/build_sandbox_image.sh"
    ).read_text()
