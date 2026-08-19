# Hosted worker host contract

This contract applies to the dedicated worker VM. Values that can drift are
loaded from `deploy/pins.json`; role defaults and paths are in
`deploy/ansible/roles/hosted_worker/defaults/main.yml`.

## Host and runtime

- Linux Ubuntu `24.04`, x86_64, with kernel at least `6.8`.
- cgroup v2 must be available.
- The pinned gVisor `runsc` release is `20260810.0`. The download URL template
  and SHA-512 pins are in `deploy/pins.json`; provenance is the upstream gVisor
  release object at that URL. The role verifies the checksum during download
  and checks `runsc --version`.
- The worker uses `/usr/local/bin/runsc`; its durable per-attempt bundle and
  runsc state directories are `/var/lib/se-skills/bundles` and
  `/var/lib/se-skills/runsc`.
- The worker invokes the root-owned Python broker at
  `/usr/local/sbin/se-skills-runsc` using the exact
  `/etc/sudoers.d/se-skills-runsc` rule, whose `""` command-argument token
  forbids all broker arguments. It accepts only a strict typed request
  on stdin; no secret or job value is placed in argv. The broker creates all
  bundles/state and writes the fixed OCI config from
  `/etc/se-skills/runsc-broker.json`; the worker cannot select rootfs,
  process argv/environment, capabilities, devices, namespaces, or arbitrary
  bind mounts.
  `runsc` is deliberately rootful (`rootless=false`); gVisor documents that
  built-in `--rootless` maps only the caller UID and cannot represent the
  image's UID 65532 without additional userns mapping helpers. The rejected
  rootless alternative would add setuid mapping infrastructure and host attack
  surface. The broker configuration is rendered from the same Ansible variables
  as `runsc`, the durable state, bundle, and staging directories; preflight
  checks the rendered broker/config/sudoers pair before allowing the worker to
  start.
  Input/output/proxy workspaces are confined to approved worker paths and
  atomically sealed into the root-owned staging parent; input is read-only and
  output remains worker-readable. List and stale cleanup use the same broker
  boundary and `list --format=text` contract with its required `ID` header.
  Run requests contain only typed operation-specific fields: `run` carries
  `container_id`, sealed workspace paths, and `job`; list carries
  `container_id` and `state_dir`; finalize carries only `container_id`; cleanup
  carries no fields and uses the root-owned configured reclaim age. Unknown
  fields and wrong types fail closed with one
  fixed diagnostic. The broker derives `RLIMIT_CPU` from the validated job
  deadline. Worker exit sentinels 65 and 66 become closed cleanup failures.
- The broker runs as `/usr/bin/python3 -I` and uses only the Python standard
  library. Isolated mode ignores `PYTHONPATH` and user-site packages. The role
  and preflight require the interpreter, broker script, and broker config to be
  `root:root` and non-group/world-writable; preflight executes this exact
  isolated interpreter to obtain its actual `sys.path` and verifies every
  resolved file or directory. The host interpreter is Ubuntu 24.04's Python
  3.12 contract; the sandbox image's Python 3.11 path is a separate image
  contract and is not used to validate the host broker.
- Broker admission is root-configured: stdin is capped at 1 MiB and five
  seconds, strings at 8 KiB, collections at 256 items, nesting at 16 levels,
  and concurrent operations at four. Run/list/finalize/cleanup are bounded by a
  30-second broker deadline; execution deadlines must be future and within
  the configured 15-minute attempt horizon.
- A root-owned journal under `/var/lib/se-skills/runsc-journal` is fsynced
  before sealing and after every sealing phase. It records custody paths,
  process identity, and phase. Broker-owned `finalize` is the only terminal
  release path; it verifies process/container absence, reconciles journal,
  staging, bundle, workspace, and state records, and removes the journal last.
  Before publication, finalize validates every output inode through
  descriptor-based no-follow operations and applies final ownership and modes
  while the tree remains root-only in staging. It then uses a no-replace
  atomic rename as the final workspace mutation; an occupied or unsafe
  destination retains the journal for recovery. The runsc child is held behind
  a broker-owned start barrier until its PID/PGID/start-time identity is
  durably journaled; a journal-write failure terminates the blocked child
  synchronously.
  Input, proxy, and job material is always discarded; output is restored only
  for a journaled successful run, and otherwise discarded to avoid restoring
  untrusted partial output. The stale sweeper invokes the same finalization
  primitive using the root-configured reclaim age. For an old journal-less or
  unusable journal record, it reclaims only root-owned state, bundle, and
  staging residue after validating the container id and proving absence with
  the broker's `runsc list` check; unverifiable entries are retained and no
  process is signaled.
- The broker configuration pins `sandbox_uid: 65532` and `sandbox_gid: 65532`,
  matching the sandbox image's `USER 65532:65532` declaration. Preflight
  verifies these deployed values against the image contract. Test-only
  user-namespace lifecycle fixtures may set both values to zero because all
  namespace ids map to the invoking test user.
- `journal_phase_pause_seconds` is a bounded, root-configured diagnostic aid
  for deterministic lifecycle testing, including observing barrier custody and
  cleanup failure windows. It is rendered as zero in production, and preflight
  rejects a non-zero value.
- The sandbox image uses the pinned Python and distroless base digests in
  `deploy/pins.json`. An approved image digest and rootfs digest must be
  populated before production preflight passes. `SANDBOX_IMAGE_DIGEST` is
  required in the worker configuration and must match both the root-owned
  manifest and any non-null approved digest in `deploy/pins.json`.
- Release tooling is pinned for Linux amd64 because this host contract is
  x86_64. Each selected release must have been public for at least seven days
  before it is recorded here: Syft `1.50.0`
  (`bf7b29ff57f06da30918266a0e1c2885a8f99784798d1bdb1628886aa015d788`),
  Grype `0.116.1`
  (`0122df7b655981abe547ad3d2190d65551dac6a2bfc80b4dc2a989b5d0587458`),
  and Cosign `3.1.3`
  (`4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71`).
- Release output is promoted under
  `/etc/se-skills/evidence/<image-digest-hex>/`. The installed manifest is
  `/etc/se-skills/sandbox-manifest.json`; it references absolute SBOM and
  provenance paths below that digest-specific directory. The release bundle
  contains evidence names, not runner-local absolute paths.

## Identity, files, and modes

The role creates the non-login system user `se-worker` (UID `995`) and group
`se-worker`. The service runs only as that identity with
`/usr/sbin/nologin`; operators do not use it for SSH.

| Path | Owner/group | Mode | Purpose |
|---|---|---:|---|
| `/opt/se-skills` | root/root | 0755 | operator-delivered application payload |
| `/opt/se-skills/rootfs` | root/root | 0755 | approved sandbox rootfs |
| `/usr/bin/python3` | root/root | executable, non-group/world-writable | isolated standard-library broker interpreter |
| `/usr/local/sbin/se-skills-runsc` | root/root | 0755 | Python broker |
| `/etc/se-skills/runsc-broker.json` | root/root | 0644 | broker-owned paths and admission limits |
| `/var/lib/se-skills` | root/root | 0750 | worker state parent |
| `/var/lib/se-skills/bundles` | root/root | 0700 | broker-owned bundle parent |
| `/var/lib/se-skills/runsc` | root/root | 0700 | broker-owned runsc state |
| `/var/lib/se-skills/runsc-staging` | root/root | 0700 | sealed workspace staging |
| `/var/lib/se-skills/runsc-journal` | root/root | 0700 | fsynced lifecycle custody journal |
| `/var/lib/se-skills/workspaces` | root/se-worker | 0730 | worker-created input/output/proxy workspaces |
| `/etc/se-skills` | root/se-worker | 0750 | policy and operator configuration |
| `/etc/se-skills/worker.env` | root/se-worker | 0640 | operator-provisioned secrets/config |
| `/etc/se-skills/firewall.nft` | root/root | 0644 | worker nftables table |
| `/etc/se-skills/sandbox-manifest.json` | root/root | non-worker-writable | approved image evidence manifest |
| `/run/se-skills` | systemd runtime | 0700 | runtime sockets |

The service uses the narrowly scoped sudo launcher transition plus
`ProtectSystem=strict`, `ProtectHome`,
private temporary storage, restricted address families, cgroup limits, and
explicit writable paths. The role installs `nftables`, `chrony`, and
`python3-venv`.

## Resource and time limits

The worker limits are CPU quota 100%, memory 2 GiB, PID/task count 64,
`NOFILE` 1,024, and file size 100,000,000 bytes. The broker sets the CPU
rlimit to the positive remaining duration until the validated job deadline.
Preflight also requires at
least 10 GiB free on the root filesystem and 2 GiB free in the temporary
filesystem. Time synchronization must be known and healthy; unknown sync state
fails closed.

## Network contract

The host firewall installs only the `inet se_skills` table and does not flush
other host tables. Input is deny-by-default except loopback, established
connections, and optional operator SSH CIDRs. Output is deny-by-default:
explicit configured IP/CIDR dependencies may be allowed, HTTPS is scoped to the
worker UID, and RFC1918, link-local, cloud-metadata, and IPv6 private ranges
are explicitly dropped. DNS and NTP destinations default to empty and must be
configured as address literals when needed.

The required outbound destinations are the worker database, private Storage,
approved registry, observability backend, time service, and DNS. Every
firewall destination must be an IP address or CIDR literal. Direct Anthropic
egress is disabled: configure `ANTHROPIC_EGRESS_PROXY_URL` for the controlled
forward proxy; its endpoint must be an IPv4 literal configured with
`HOSTED_ANTHROPIC_PROXY_HOST` and `HOSTED_ANTHROPIC_PROXY_PORT` in the worker
environment and the corresponding role variables. IPv6 proxy endpoints are
rejected by the host contract.
The proxy must enforce
`CONNECT`/TLS only to `api.anthropic.com:443`, resolve DNS at the proxy,
authenticate the worker through operator-owned network identity such as mTLS
or an equivalent mechanism, bound timeouts and concurrency, redact audit logs,
and omit request/response bodies. The configured URL carries no credentials.
This authentication direction is decided but not yet implemented in the
worker: there is no client-certificate or trust-bundle wiring and no live
handshake test. Provisioning, trust-bundle/client-certificate wiring, and
rotation remain 5B2B2 work.
An unset destination produces no firewall accept rule.
`HOSTED_APPROVED_HTTPS_DESTINATIONS` must match the worker-scoped destinations
rendered by the firewall on TCP/443; the proxy host/port is verified separately.
Host-level static IP
allowlisting is defense in depth; the worker proxy's TLS hostname verification
and request policy remain authoritative.

## Secrets and data handling

The role never templates secret values. Operators provision `/etc/se-skills/worker.env`
through the approved secret-management process. It contains the worker database
URL, model-proxy secret, Anthropic key, rootfs/image references, and
provider-specific configuration, including `ANTHROPIC_EGRESS_PROXY_URL`. Secret rotation is owned by the operational
owner selected in 5B2B2; rotate in the secret manager, replace the environment
file atomically, and restart the worker.

Bootstrap logs must not contain customer data, secrets, prompts, transcripts,
model responses, Storage paths/object keys, or capability/lease tokens. Worker
logs contain only redacted categories and derived identifiers.

## Verification, rollback, and recovery

The image release script produces an SBOM, vulnerability-scan results, a
registry manifest digest, digest-bound provenance, and a canonical rootfs
digest manifest. The operator installs the root-owned sandbox manifest with
the approved image/rootfs digests, SBOM/provenance paths, image reference, and
cosign identity/key/certificate expectations. Production preflight verifies
ownership and modes for every evidence file and its parent directory, hashes the materialized rootfs,
and runs the manifest-built signature command. A worker-writable manifest or
evidence file fails closed. Promotion and rollback are operator
gates; rollback means selecting a previously approved digest and restarting
the worker, not rebuilding from an unpinned tag. Extract retained rootfs
archives as root with `--same-owner --numeric-owner` so uid/gid inputs remain
consistent with the canonical digest. Release builds do not extract the image
as root: they hash and retain the Docker-export tar stream directly. Root-only
extraction is a host deployment operation only.
The verified CycloneDX attestation must match the installed SBOM byte-for-byte
after canonical JSON serialization. Verified SLSA provenance carries the
canonical rootfs digest plus source repository/commit and is checked against
both the installed manifest and deployed rootfs.
Release signing remains `workflow_dispatch`-only, requires the literal
`BUILD_SANDBOX_IMAGE` confirmation token and protected `sandbox-release`
environment, and is accepted only from `refs/heads/main` or an approved
immutable `v*` tag.
The deterministic pull-request workflow downloads and checksum-verifies
actionlint 1.7.7 (released 2025-01-19) before linting every workflow file.
The Linux-amd64 archive SHA-256 is
`023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757`,
read from the upstream
`actionlint_1.7.7_checksums.txt` release file.
Evidence installation must be run from the repository root as
`python -m scripts.install_sandbox_evidence ...` under root; its manifest is
read through the same bounded no-follow descriptor path as its evidence files.

Postgres is the source of truth for jobs, leases, attempts, and tombstones;
private object storage is the source of truth for transcripts and outputs.
Backup, restore testing, retention, disaster-recovery objectives, and the
provider-specific backup owner remain 5B2B2 decisions. The beta is limited to
one organization and one worker capacity envelope until those decisions are
approved.
