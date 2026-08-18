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
- The worker uses `/usr/local/bin/runsc`; its root/state directories are under
  `/var/lib/se-skills`.
- The sandbox image uses the pinned Python and distroless base digests in
  `deploy/pins.json`. An approved image digest and rootfs digest must be
  populated before production preflight passes.

## Identity, files, and modes

The role creates the non-login system user `se-worker` (UID `995`) and group
`se-worker`. The service runs only as that identity with
`/usr/sbin/nologin`; operators do not use it for SSH.

| Path | Owner/group | Mode | Purpose |
|---|---|---:|---|
| `/opt/se-skills` | root/root | 0755 | operator-delivered application payload |
| `/opt/se-skills/rootfs` | root/root | 0755 | approved sandbox rootfs |
| `/opt/se-skills/venv` | operator-delivered | role-created | pinned worker dependencies |
| `/var/lib/se-skills` | se-worker/se-worker | 0750 | worker state |
| `/var/lib/se-skills/bundles` | se-worker/se-worker | 0750 | ephemeral bundle parent |
| `/var/lib/se-skills/runsc` | se-worker/se-worker | 0700 | runsc state |
| `/etc/se-skills` | root/se-worker | 0750 | policy and operator configuration |
| `/etc/se-skills/worker.env` | root/se-worker | 0640 | operator-provisioned secrets/config |
| `/etc/se-skills/firewall.nft` | root/root | 0644 | worker nftables table |
| `/run/se-skills` | systemd runtime | 0700 | runtime sockets |

The service uses `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
private temporary storage, restricted address families, cgroup limits, and
explicit writable paths. The role installs `nftables`, `chrony`, and
`python3-venv`.

## Resource and time limits

The worker limits are CPU quota 100%, memory 2 GiB, PID/task count 64,
`NOFILE` 1,024, and file size 100,000,000 bytes. Preflight also requires at
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
approved registry, observability backend, time service, DNS, and Anthropic API
as approved during 5B2B2. Host-level static IP allowlisting is defense in depth;
the worker proxy's HTTPS hostname and request policy remain authoritative for
changing provider addresses.

## Secrets and data handling

The role never templates secret values. Operators provision `/etc/se-skills/worker.env`
through the approved secret-management process. It contains the worker database
URL, model-proxy secret, Anthropic key, rootfs/image references, and
provider-specific configuration. Secret rotation is owned by the operational
owner selected in 5B2B2; rotate in the secret manager, replace the environment
file atomically, and restart the worker.

Bootstrap logs must not contain customer data, secrets, prompts, transcripts,
model responses, Storage paths/object keys, or capability/lease tokens. Worker
logs contain only redacted categories and derived identifiers.

## Verification, rollback, and recovery

The image release script produces an SBOM, vulnerability-scan results, a
registry manifest digest, and a rootfs digest manifest. Production preflight
requires the configured approved digests. Promotion and rollback are operator
gates; rollback means selecting a previously approved digest and restarting
the worker, not rebuilding from an unpinned tag.

Postgres is the source of truth for jobs, leases, attempts, and tombstones;
private object storage is the source of truth for transcripts and outputs.
Backup, restore testing, retention, disaster-recovery objectives, and the
provider-specific backup owner remain 5B2B2 decisions. The beta is limited to
one organization and one worker capacity envelope until those decisions are
approved.
