# ADR-006: Hosted beta deployment topology

**Status:** Accepted for the beta foundation  
**Date:** 2026-08-18

## Decision

The first hosted beta will run one dedicated SE Skills worker process on one
hardened Ubuntu 24.04 x86_64 VM. The cloud provider, account, project, region,
and network are intentionally not selected in this PR.

The worker has a bounded capacity contract: one worker process, a 100% CPU
quota, 2 GiB memory maximum, 64 tasks, and 1,024 open files. The authoritative
values are in `deploy/pins.json` and are consumed by the preflight and Ansible
role. Queue leases and bounded retries provide recovery when the process or VM
fails.

Each attempt starts one ephemeral gVisor `runsc` sandbox. The worker resolves
authorized inputs, mounts them read-only, and validates the output outside the
sandbox. Model calls travel from the sandbox to one worker-owned per-attempt
model proxy over a Unix domain socket. The sandbox never receives the model
credential.

The worker host has no public application port. FastAPI may run elsewhere; this
topology only defines the worker boundary.

## Rejected alternatives

- **Kubernetes for the first beta:** adds cluster, node, network-policy, and
  image-scheduling dependencies before the single-organization workload needs
  them.
- **Ordinary Docker/runc fallback:** does not provide the kernel and filesystem
  isolation required for hostile transcript input. Production startup fails
  closed rather than silently selecting it.
- **Firecracker before beta:** offers stronger VM isolation but requires a
  separate image, jailer, kernel, and operational program. The runtime contract
  can move to Firecracker later without changing the worker/orchestrator
  boundary.

The design is intended to port to a managed gVisor service later. Portability
does not mean that this PR provisions that service.

## Consequences

This choice keeps the beta operational surface small and makes the trust
boundaries explicit. It also leaves a single-worker capacity ceiling, requires
host maintenance, and defers provider-specific networking, secrets, registry,
and observability decisions to 5B2B2.
