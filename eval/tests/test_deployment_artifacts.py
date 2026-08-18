from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
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
        "NoNewPrivileges=no",
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
    assert "fd00::/8" in firewall
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
    for version in ("0.30.0", "2.10.0", "2.0", "0.28.1", "0.100", "0.30.0"):
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


def test_broker_uses_isolated_standard_library_interpreter() -> None:
    broker = (ROOT / "scripts/runsc_broker.py").read_text()
    assert broker.splitlines()[0] == "#!/usr/bin/python3 -I"
    assert "pydantic" not in broker

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json, sys; assert json and sys.flags.isolated",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_broker_sandbox_identity_matches_image_contract() -> None:
    defaults = (
        ANSIBLE / "roles" / "hosted_worker" / "defaults" / "main.yml"
    ).read_text()
    template = (
        ANSIBLE
        / "roles"
        / "hosted_worker"
        / "templates"
        / "runsc-broker.json.j2"
    ).read_text()
    image = (ROOT / "webapp/hosted/runsc/Dockerfile").read_text()
    assert "hosted_sandbox_uid: 65532" in defaults
    assert "hosted_sandbox_gid: 65532" in defaults
    assert '"sandbox_uid": {{ hosted_sandbox_uid }}' in template
    assert '"sandbox_gid": {{ hosted_sandbox_gid }}' in template
    assert "USER 65532:65532" in image


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
        "/etc/systemd/system/se-skills-firewall.service",
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
    assert "--same-owner" not in script
    assert "--numeric-owner" not in script
    assert "--same-owner" in (ROOT / "docs/HOST_CONTRACT.md").read_text()
    assert "--numeric-owner" in (ROOT / "docs/HOST_CONTRACT.md").read_text()
    assert "--owner=0" not in script
    assert "--group=0" not in script
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
    on_block = workflow.get("on", workflow.get(True, {}))
    assert "${{" not in yaml.safe_dump(on_block)
    image_input = on_block["workflow_dispatch"]["inputs"]["image"]
    assert image_input["required"] is True
    assert "default" not in image_input
    assert "<owner>/<repo>/se-skills-sandbox" in image_input["description"]
    assert "docker push" in (
        ROOT / "deploy/images/build_sandbox_image.sh"
    ).read_text()
    identity = re.search(
        r"CERTIFICATE_IDENTITY:\s+(.+)", text
    )
    assert identity is not None
    assert (
        identity.group(1)
        == "https://github.com/${{ github.repository }}/.github/workflows/"
        "sandbox-image-release.yml@${{ github.ref }}"
    )


def test_release_workflow_gates_signing_to_approved_refs() -> None:
    text = (ROOT / ".github/workflows/sandbox-image-release.yml").read_text()
    assert re.search(
        r"https://github\.com/\$\{\{\s*github\.repository\s*\}\}/"
        r"\.github/workflows/sandbox-image-release\.yml@\$\{\{\s*github\.ref\s*\}\}",
        text,
    )
    assert text.index("Validate approved release ref") < text.index(
        "Build and verify sandbox image"
    )
    assert text.index("Validate image namespace") < text.index(
        "Build and verify sandbox image"
    )
    assert '"$REPOSITORY/se-skills-sandbox"' in text
    assert text.index("Verify approved release commit") < text.index(
        "Build and verify sandbox image"
    )
    assert "fetch-depth: 0" in text
    assert "git merge-base --is-ancestor" in text
    assert "refs/heads/main|refs/tags/v[0-9]*.[0-9]*.[0-9]*" in text


def test_release_workflow_derives_verified_tools_and_retains_evidence() -> None:
    text = (ROOT / ".github/workflows/sandbox-image-release.yml").read_text()
    pins = (ROOT / "deploy/pins.json").read_text()

    assert "mapfile -t pin_values" in text
    assert "sha256sum --check" in text
    assert text.index("sha256sum --check") < text.index("chmod 0755")
    assert "$syft_filename" in text
    assert "$grype_filename" in text
    assert "$cosign_filename" in text
    assert "tools/cosign-linux-amd64" not in text
    build = (ROOT / "deploy/images/build_sandbox_image.sh").read_text()
    assert '"image_digest": sys.argv[2]' in build
    assert "cosign attest" in build
    assert "registry_digest" in build
    assert 'cosign attest --yes --type cyclonedx --predicate' in build
    assert 'cosign attest --yes --type slsaprovenance --predicate' in build
    assert '"${IMAGE}@${registry_digest}"' in build
    assert '"image_digest": sys.argv[2]' in build
    assert '"sbom_name"' in build
    assert '"provenance_name"' in build
    assert "metadata.component" in build
    assert "rootfs.tar.gz" in build
    assert "path: deploy/images/out/*" in text
    assert '"build_tools"' in pins


def test_actionlint_pin_and_local_workflow_command_are_present() -> None:
    pins = yaml.safe_load((ROOT / "deploy/pins.json").read_text())
    actionlint = pins["build_tools"]["actionlint"]
    assert actionlint["version"] == "1.7.7"
    assert actionlint["sha256"] == "023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757"
    command = (ROOT / "scripts/check-workflows.sh").read_text()
    assert "find" in command and "actionlint" in command


def test_pinned_actionlint_rejects_old_input_context_fixture_when_available() -> None:
    binary = Path(os.environ.get("ACTIONLINT_BIN", ROOT / ".tools/actionlint"))
    if not binary.is_file():
        pytest.skip("pinned actionlint binary is unavailable")
    invalid = subprocess.run(
        [str(binary), str(ROOT / "eval/fixtures/invalid-workflow-input-default.yml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert 'context "github" is not allowed here' in invalid.stdout
    corrected = subprocess.run(
        [str(binary), *map(str, sorted((ROOT / ".github/workflows").glob("*.yml")))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert corrected.returncode == 0, corrected.stderr


def test_runsc_helper_rejects_flags_not_emitted_by_worker(tmp_path: Path) -> None:
    helper = ROOT / "scripts/runsc_broker.py"
    result = subprocess.run(
        [sys.executable, str(helper)],
        input=(
            '{"operation":"list","container_id":"se-aaaaaaaaaaaa",'
            '"state_dir":"/tmp/runsc","unknown":"field"}'
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_firewall_is_applied_before_worker_start_and_as_one_transaction() -> None:
    main = yaml.safe_load(
        (ANSIBLE / "roles/hosted_worker/tasks/main.yml").read_text()
    )
    names = [
        task["ansible.builtin.include_tasks"]
        for task in main
        if "ansible.builtin.include_tasks" in task
    ]
    assert names.index("firewall.yml") < names.index("systemd.yml")

    firewall = yaml.safe_load(
        (ANSIBLE / "roles/hosted_worker/tasks/firewall.yml").read_text()
    )
    commands = [
        task["ansible.builtin.command"]["argv"]
        for task in firewall
        if "ansible.builtin.command" in task
    ]
    assert ["nft", "--check", "-f", "{{ hosted_firewall_path }}"] in commands
    assert ["nft", "-f", "{{ hosted_firewall_path }}"] in commands
    apply_task = next(
        task
        for task in firewall
        if task.get("name") == "Apply rendered firewall policy atomically"
    )
    assert "failed_when" not in apply_task
    policy = (
        ANSIBLE / "roles/hosted_worker/templates/firewall.nft.j2"
    ).read_text()
    delete_index = policy.index("delete table inet se_skills")
    assert policy.index("table inet se_skills {", delete_index) > delete_index
    service = (
        ANSIBLE
        / "roles/hosted_worker/templates/se-skills-firewall.service.j2"
    ).read_text()
    assert "ExecStart=/usr/sbin/nft -f {{ hosted_firewall_path }}" in service
    assert "RemainAfterExit=yes" in service
    worker = (
        ANSIBLE
        / "roles/hosted_worker/templates/se-skills-worker.service.j2"
    ).read_text()
    assert "Requires={{ hosted_firewall_unit }}" in worker
    assert "After=network-online.target {{ hosted_firewall_unit }}" in worker
    enable_task = next(
        task
        for task in firewall
        if task.get("name") == "Enable persistent hosted firewall unit"
    )
    assert enable_task["ansible.builtin.systemd"]["enabled"] is True
    assert enable_task["ansible.builtin.systemd"]["state"] == "started"
    assert "nftables" not in "\n".join(task.get("name", "") for task in firewall)


def test_cleanup_unit_runs_as_worker_and_state_modes_match_preflight() -> None:
    unit = (
        ANSIBLE
        / "roles/hosted_worker/templates/se-skills-cleanup.service.j2"
    ).read_text()
    assert "User={{ hosted_worker_user }}" in unit
    assert "Group={{ hosted_worker_group }}" in unit
    directories = (
        ANSIBLE / "roles/hosted_worker/tasks/directories.yml"
    ).read_text()
    assert 'mode: "{{ item.mode }}"' in directories
    assert 'mode: "0750"' in directories
    assert 'mode: "0700"' in directories
