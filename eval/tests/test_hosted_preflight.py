from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess
from typing import Sequence

import pytest

from webapp.hosted.preflight import (
    ListeningSocket,
    PathFacts,
    PreflightConfig,
    run_preflight,
)
from webapp.hosted.firewall_policy import evaluate_output_policy, parse_output_policy
from webapp.hosted.pins import load_pins
from webapp.hosted.supply_chain import (
    SupplyChainArtifacts,
    SupplyChainCommandResult,
    verify_supply_chain,
)


class _Probe:
    def __init__(self) -> None:
        self.paths = {
            "/usr/local/bin/runsc": PathFacts(
                True,
                "root",
                "root",
                0o755,
                True,
                False,
                load_pins().runsc_checksum("x86_64"),
            ),
            "/var/lib/se-skills": PathFacts(
                True, "se-worker", "se-worker", 0o750, False, True
            ),
            "/var/lib/se-skills/runsc": PathFacts(
                True, "se-worker", "se-worker", 0o700, False, True
            ),
            "/var/lib/se-skills/bundles": PathFacts(
                True, "se-worker", "se-worker", 0o700, False, True
            ),
            "/opt/rootfs": PathFacts(
                True, "root", "root", 0o755, False, True
            ),
        }
        self.public = False
        self.namespace = frozenset({"pid", "net", "user"})
        self.clock = 0.1
        self.free = 20_000_000_000
        self.firewall = "policy drop\n169.254.169.254\nfd00:ec2::254"
        self.firewall_rules = """table inet se_skills {
  chain input {
    type filter hook input priority 0; policy drop;
    iifname "lo" accept
    ct state established,related accept
  }
  chain output {
    type filter hook output priority 0; policy drop;
    oifname "lo" accept
    ct state established,related accept
    ip daddr 169.254.169.254 drop
    ip daddr 169.254.0.0/16 drop
    ip6 daddr fe80::/10 drop
    ip6 daddr fd00::/8 drop
    ip6 daddr fd00:ec2::254 drop
    ip daddr 10.0.0.0/8 drop
    ip daddr 172.16.0.0/12 drop
    ip daddr 192.168.0.0/16 drop
    ip6 daddr fc00::/7 drop
    meta skuid 995 ip daddr 203.0.113.10 tcp dport 443 accept
  }
}"""

    def os_release(self) -> tuple[str, str]:
        return "ubuntu", "24.04"

    def kernel_version(self) -> str:
        return "6.8.0"

    def architecture(self) -> str:
        return "x86_64"

    def path_info(self, path: str) -> PathFacts:
        return self.paths.get(path, PathFacts(False, None, None, None, False, False))

    def command(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        if argv[:2] == ("nft", "list"):
            return SupplyChainCommandResult(returncode=0, stdout=self.firewall_rules)
        return SupplyChainCommandResult(returncode=0, stdout="runsc 20260810.0")

    def cgroup_version(self) -> int | None:
        return 2

    def namespace_features(self) -> frozenset[str]:
        return self.namespace

    def listening_sockets(self) -> tuple[ListeningSocket, ...] | None:
        return (
            (ListeningSocket("0.0.0.0", 0),)
            if self.public
            else (ListeningSocket("127.0.0.1", 0),)
        )

    def clock_offset_seconds(self) -> float | None:
        return self.clock

    def disk_free_bytes(self, path: str) -> int | None:
        return self.free

    def file_text(self, path: str) -> str | None:
        return self.firewall


class _CommandRunner:
    def run(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        return SupplyChainCommandResult(returncode=0)


def _supply_chain():
    digest = "sha256:" + "a" * 64
    rootfs = "sha256:" + "b" * 64
    return verify_supply_chain(
        SupplyChainArtifacts(
            image_digest=digest,
            approved_image_digest=digest,
            sbom_text="{}",
            sbom_image_digest=digest,
            provenance_image_digest=digest,
            rootfs_digest=rootfs,
            manifest_rootfs_digest=rootfs,
            signature_command=("verify",),
            sbom_attestation_command=("verify-attestation", "cyclonedx"),
            provenance_attestation_command=("verify-attestation", "slsaprovenance"),
        ),
        _CommandRunner(),
    )


def _config(**updates: object) -> PreflightConfig:
    base = dict(
        pins=load_pins(),
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
        model_proxy_secret="Abcdefghijklmnopqrstuvwxyz012345",
        anthropic_proxy_url="http://203.0.113.10:3128",
        database_url="postgresql://worker@db/app?sslmode=require",
        storage_url="https://storage.example",
        rootfs_path="/opt/rootfs",
        supply_chain=_supply_chain(),
    )
    base.update(updates)
    base.setdefault("approved_https_destinations", frozenset({"203.0.113.10"}))
    return PreflightConfig(**base)


def _failed(report, check_id: str) -> bool:
    return any(check.check_id == check_id and check.status == "fail" for check in report.checks)


def test_compliant_host_passes() -> None:
    report = run_preflight(_config(), _Probe())

    assert report.ok


@pytest.mark.parametrize(
    ("updates", "check_id"),
    [
        (
            {
                "pins": load_pins().model_copy(
                    update={
                        "runsc": load_pins().runsc.model_copy(
                            update={"sha512": {"x86_64": "0" * 128}}
                        )
                    }
                )
            },
            "runsc_checksum",
        ),
        (
            {
                "pins": load_pins().model_copy(
                    update={
                        "runsc": load_pins().runsc.model_copy(
                            update={"version": "20270101.0"}
                        )
                    }
                )
            },
            "runsc_version",
        ),
        ({"model_proxy_secret": "a" * 32}, "model_proxy_secret"),
        ({"anthropic_api_url": "http://api.anthropic.com"}, "anthropic_url"),
        ({"anthropic_proxy_url": ""}, "anthropic_egress_proxy"),
        ({"database_url": "postgresql://worker@db/app"}, "database_url"),
        ({"storage_url": "http://storage.example"}, "storage_url"),
        ({"storage_url": "https://user:password@storage.example"}, "storage_url"),
        ({"supply_chain": None}, "supply_chain"),
    ],
)
def test_preflight_rejects_configuration_failures(
    updates: dict[str, object], check_id: str
) -> None:
    assert _failed(run_preflight(_config(**updates), _Probe()), check_id)


def test_preflight_rejects_unsafe_runsc_path() -> None:
    probe = _Probe()
    probe.paths["/usr/local/bin/runsc"] = PathFacts(
        True, "se-worker", "se-worker", 0o777, True, False, "f" * 128
    )

    report = run_preflight(_config(), probe)

    assert _failed(report, "runsc_binary")


def test_preflight_rejects_missing_namespaces_and_public_listener() -> None:
    probe = _Probe()
    probe.namespace = frozenset({"pid"})
    probe.public = True

    report = run_preflight(_config(), probe)

    assert _failed(report, "namespace_features")
    assert _failed(report, "public_listener")


def test_preflight_rejects_clock_disk_and_firewall_failures() -> None:
    probe = _Probe()
    probe.clock = 2.0
    probe.free = 1
    probe.firewall_rules = "accept"

    report = run_preflight(_config(), probe)

    assert _failed(report, "clock_sync")
    assert _failed(report, "root_disk_free")
    assert _failed(report, "temp_disk_free")
    assert _failed(report, "firewall_policy")


def test_preflight_output_is_redacted() -> None:
    secret = "SecretValueNeverPrinted0123456789"
    report = run_preflight(
        _config(model_proxy_secret=secret, present_config_names=frozenset()),
        _Probe(),
    )
    rendered = report.model_dump_json()

    assert secret not in rendered
    assert "DATABASE_WORKER_URL" in rendered


def test_unknown_clock_offset_fails_closed() -> None:
    probe = _Probe()
    probe.clock = None

    report = run_preflight(_config(), probe)

    assert _failed(report, "clock_sync")


def test_listener_enumeration_failure_fails_closed() -> None:
    probe = _Probe()
    probe.listening_sockets = lambda: None

    report = run_preflight(_config(), probe)

    assert _failed(report, "public_listener")


def test_production_runtime_policy_rejects_echo() -> None:
    report = run_preflight(
        _config(hosted_env="production", runtime="echo"),
        _Probe(),
    )

    assert _failed(report, "production_runtime_policy")


def test_production_entrypoint_rejects_echo_before_any_offline_path() -> None:
    environment = os.environ.copy()
    environment.update({"HOSTED_MODE": "1", "HOSTED_ENV": "production"})
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/run_hosted_worker.py",
            "--runtime",
            "echo",
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 1
    assert "EchoExecutor is not allowed in production" in result.stderr
    assert "offline" not in result.stderr.lower()


def test_shipped_firewall_template_matches_preflight_contract() -> None:
    template = Path(
        "deploy/ansible/roles/hosted_worker/templates/firewall.nft.j2"
    ).read_text()
    probe = _Probe()
    probe.firewall_rules = _render_firewall_template(template)

    report = run_preflight(
        _config(
            approved_https_destinations=frozenset(
                {"203.0.113.10", "198.51.100.12", "198.51.100.13", "198.51.100.14"}
            )
        ),
        probe,
    )

    assert not _failed(report, "firewall_policy")
    parsed = parse_output_policy(probe.firewall_rules)
    for destination in (
        "203.0.113.10",
        "198.51.100.12",
        "198.51.100.13",
        "198.51.100.14",
    ):
        assert evaluate_output_policy(parsed, 995, destination, "tcp", 443) == "accept"
        assert evaluate_output_policy(parsed, 994, destination, "tcp", 443) == "drop"
    assert evaluate_output_policy(parsed, 995, "93.184.216.34", "tcp", 443) == "drop"
    assert evaluate_output_policy(parsed, 995, "10.0.0.5", "tcp", 443) == "drop"
    assert evaluate_output_policy(parsed, 995, "fd00::5", "tcp", 443) == "drop"
    for metadata in ("169.254.169.254", "169.254.1.1", "fe80::1"):
        assert evaluate_output_policy(parsed, 995, metadata, "tcp", 443) == "drop"
    assert evaluate_output_policy(parsed, 995, "192.0.2.53", "udp", 53) == "accept"
    assert evaluate_output_policy(parsed, 994, "192.0.2.53", "udp", 53) == "drop"
    assert evaluate_output_policy(parsed, 995, "192.0.2.123", "udp", 123) == "accept"
    assert evaluate_output_policy(parsed, 994, "192.0.2.123", "udp", 123) == "drop"


def _render_firewall_template(template: str) -> str:
    values = {
        "hosted_operator_ssh_cidr": "",
        "hosted_worker_uid": 995,
        "hosted_dns_servers": ["192.0.2.53", "2001:db8::53"],
        "hosted_ntp_servers": ["192.0.2.123", "2001:db8::123"],
        "hosted_database_host": "198.51.100.10",
        "hosted_storage_host": "203.0.113.10",
        "hosted_registry_host": "198.51.100.12",
        "hosted_observability_host": "198.51.100.13",
        "hosted_anthropic_proxy_host": "198.51.100.14",
        "hosted_anthropic_proxy_port": 443,
        "hosted_postgres_port": 5432,
        "hosted_storage_port": 443,
        "hosted_registry_port": 443,
        "hosted_observability_port": 443,
    }
    try:
        from jinja2 import Template
    except ImportError:
        return _render_firewall_without_jinja(template, values)
    return Template(template).render(**values)


def _render_firewall_without_jinja(template: str, values: dict[str, object]) -> str:
    rendered = template
    for name in ("hosted_dns_servers", "hosted_ntp_servers"):
        pattern = re.compile(
            rf"\{{% for server in {name} %\}}(.*?)\{{% endfor %\}}",
            re.DOTALL,
        )
        body_match = pattern.search(rendered)
        assert body_match is not None
        body = body_match.group(1)
        replacement = "".join(
            body.replace("{{ server }}", str(server))
            for server in values[name]  # type: ignore[union-attr]
        )
        rendered = pattern.sub(replacement, rendered, count=1)
    for name in (
        "hosted_operator_ssh_cidr",
        "hosted_database_host",
        "hosted_storage_host",
        "hosted_registry_host",
        "hosted_observability_host",
        "hosted_anthropic_proxy_host",
    ):
        pattern = re.compile(
            rf"\{{% if {name} \| length > 0 %\}}(.*?)\{{% endif %\}}",
            re.DOTALL,
        )
        rendered = pattern.sub(
            lambda match: match.group(1) if values[name] else "",
            rendered,
            count=1,
        )
    for name, value in values.items():
        rendered = re.sub(
            rf"\{{\{{\s*{name}\s*\}}\}}", str(value), rendered
        )
    return re.sub(r"\{{%.*?%\}}", "", rendered, flags=re.DOTALL)
