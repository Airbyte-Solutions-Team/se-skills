from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from webapp.hosted.firewall_policy import evaluate_output_policy, parse_output_policy
from webapp.hosted.rootfs_digest import digest_rootfs
from webapp.hosted.supply_chain_manifest import load_artifact_manifest
from webapp.hosted.preflight import PathFacts
from webapp.hosted.supply_chain import SupplyChainCommandResult


def test_rootfs_digest_changes_for_materialized_tree_tampering(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    (rootfs / "bin").mkdir()
    (rootfs / "bin" / "app").write_text("safe", encoding="utf-8")
    (rootfs / "link").symlink_to("bin/app")
    original = digest_rootfs(rootfs)

    (rootfs / "bin" / "app").write_text("tampered", encoding="utf-8")
    assert digest_rootfs(rootfs) != original
    (rootfs / "bin" / "app").write_text("safe", encoding="utf-8")
    (rootfs / "bin" / "app").chmod(0o700)
    assert digest_rootfs(rootfs) != original
    (rootfs / "bin" / "app").chmod(0o644)
    (rootfs / "added").write_text("new", encoding="utf-8")
    assert digest_rootfs(rootfs) != original
    (rootfs / "added").unlink()
    (rootfs / "bin" / "app").unlink()
    assert digest_rootfs(rootfs) != original
    (rootfs / "bin" / "app").write_text("safe", encoding="utf-8")
    (rootfs / "link").unlink()
    (rootfs / "link").symlink_to("bin")
    assert digest_rootfs(rootfs) != original


def test_firewall_policy_evaluates_ordered_boundary() -> None:
    rules = parse_output_policy(
        """table inet se_skills {
  chain output {
    type filter hook output priority 0; policy drop;
    ip daddr 169.254.169.254 drop
    ip daddr 10.0.0.0/8 drop
    ip6 daddr fc00::/7 drop
    meta skuid 995 ip daddr 203.0.113.10 tcp dport 443 accept
  }
}"""
    )

    assert evaluate_output_policy(rules, 995, "203.0.113.10", "tcp", 443) == "accept"
    assert evaluate_output_policy(rules, 994, "203.0.113.10", "tcp", 443) == "drop"
    assert evaluate_output_policy(rules, 995, "10.0.0.5", "tcp", 443) == "drop"
    assert evaluate_output_policy(rules, 995, "169.254.169.254", "tcp", 443) == "drop"
    assert evaluate_output_policy(rules, 995, "192.0.2.10", "tcp", 443) == "drop"


def test_firewall_policy_scopes_dns_and_ntp_to_worker_and_destinations() -> None:
    rules = parse_output_policy(
        """table inet se_skills {
  chain input {
    type filter hook input priority 0; policy drop;
  }
  chain output {
    type filter hook output priority 0; policy drop;
    meta skuid 995 ip daddr 192.0.2.53 udp dport 53 accept
    meta skuid 995 ip daddr 192.0.2.123 udp dport 123 accept
  }
}"""
    )

    assert evaluate_output_policy(rules, 995, "192.0.2.53", "udp", 53) == "accept"
    assert evaluate_output_policy(rules, 994, "192.0.2.53", "udp", 53) == "drop"
    assert evaluate_output_policy(rules, 995, "192.0.2.54", "udp", 53) == "drop"
    assert evaluate_output_policy(rules, 995, "192.0.2.123", "udp", 123) == "accept"


def test_firewall_policy_rejects_unknown_rules_and_accept_chain_policy() -> None:
    unknown = parse_output_policy(
        """table inet se_skills {
  chain input {
    type filter hook input priority 0; policy drop;
  }
  chain output {
    type filter hook output priority 0; policy drop;
    return
  }
}"""
    )
    accepted_input = parse_output_policy(
        """table inet se_skills {
  chain input {
    type filter hook input priority 0; policy accept;
  }
  chain output {
    type filter hook output priority 0; policy drop;
  }
}"""
    )

    assert not unknown.valid
    assert not accepted_input.valid


class _ManifestProbe:
    def __init__(self, files: dict[str, str], unsafe: set[str] | None = None) -> None:
        self.files = files
        self.unsafe = unsafe or set()

    def path_info(self, path: str) -> PathFacts:
        is_parent = any(Path(item).parent.as_posix() == path for item in self.files)
        return PathFacts(
            exists=path in self.files or is_parent,
            owner="se-worker" if path in self.unsafe else "root",
            group="root",
            mode=0o644,
            is_file=path in self.files,
            is_directory=is_parent,
        )

    def file_text(self, path: str) -> str | None:
        return self.files.get(path)


def test_manifest_loader_populates_evidence_and_rejects_worker_writable_manifest(
    tmp_path: Path,
) -> None:
    digest = "sha256:" + "a" * 64
    manifest_path = str(tmp_path / "manifest.json")
    sbom_path = str(tmp_path / "sbom.json")
    provenance_path = str(tmp_path / "provenance.json")
    manifest = {
        "image_digest": digest,
        "rootfs_digest": "sha256:" + "b" * 64,
        "sbom_path": sbom_path,
        "provenance_path": provenance_path,
        "signature": {
            "image_reference": "registry.example/se-skills@sha256:" + "a" * 64,
            "certificate_identity": "release@example.invalid",
            "certificate_oidc_issuer": "https://issuer.example",
        },
    }
    files = {
        manifest_path: json.dumps(manifest),
        sbom_path: json.dumps({"subject": [{"digest": digest}]}),
        provenance_path: json.dumps({"subject": [{"digest": digest}]}),
    }
    loaded = load_artifact_manifest(
        Path(manifest_path), _ManifestProbe(files)
    )

    assert loaded.trusted
    assert loaded.artifacts is not None
    assert loaded.artifacts.signature_command[-1].startswith("registry.example/")
    rejected = load_artifact_manifest(
        Path(manifest_path),
        _ManifestProbe(files, unsafe={manifest_path}),
    )
    assert not rejected.trusted
    assert "unsafe" in rejected.detail


def test_cleanup_script_removes_only_verified_dead_state(tmp_path: Path) -> None:
    root = tmp_path / "runsc"
    bundles = tmp_path / "bundles"
    root.mkdir()
    bundles.mkdir()
    (root / "dead").mkdir()
    (root / "live").mkdir()
    (bundles / "dead").mkdir()
    (bundles / "dead" / "job.json").write_text("{}", encoding="utf-8")
    (bundles / "live").mkdir()
    (bundles / "live" / "job.json").write_text("{}", encoding="utf-8")
    (bundles / "orphan").mkdir()
    shim = tmp_path / "runsc-shim"
    shim.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'--root=" + str(root / "dead") + " list'*) printf 'ID STATUS\\n';;\n"
        "  *'--root=" + str(root / "live") + " list'*) printf 'ID STATUS\\nlive running\\n';;\n"
        "  *'--root=" + str(root / "dead") + " delete'*) exit 0;;\n"
        "  *) exit 1;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    template = Path(
        "deploy/ansible/roles/hosted_worker/templates/cleanup-stale-sandboxes.sh.j2"
    ).read_text(encoding="utf-8")
    rendered = (
        template.replace("{{ hosted_runsc_state_dir }}", str(root))
        .replace("{{ hosted_bundle_dir }}", str(bundles))
        .replace("{{ hosted_runsc_binary }}", str(shim))
    )
    script = tmp_path / "cleanup.sh"
    script.write_text(rendered, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    subprocess.run([str(script)], check=True, env={**os.environ, "PATH": "/usr/bin:/bin"})

    assert not (root / "dead").exists()
    assert (root / "live").exists()
    assert (bundles / "dead").exists()
    assert (bundles / "live").exists()
    assert not (bundles / "orphan").exists()
