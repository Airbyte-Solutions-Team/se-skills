# ADR-005: Slice 5B Hosted Runtime and Sandbox Decision

| | |
|---|---|
| **Status** | Accepted — core runtime decision operative for Slices 5B1, 5B2A, and the 5B2B1 deployment foundation; real host provisioning and live Anthropic smoke testing remain Slice 5B2B2 |
| **Date** | 2026-08-11 |
| **Deciders** | Devin (implementation), requester (review) |
| **Applies to** | Slice 5B1 (trusted worker-side orchestration) and Slice 5B2 (gVisor sandbox + production model proxy) |

## Context

Slice 4 gives us a durable Postgres job queue, a least-privilege worker role, expiring leases, and a deterministic `EchoExecutor`. Slice 5B must run a real `post-call` skill inside an isolated sandbox without giving the sandbox access to Postgres, Supabase Storage credentials, model keys, or the local filesystem. The decision is hard to reverse because it shapes the trust boundary, credential model, local testing strategy, and operational cost for the beta.

This ADR evaluates four candidate approaches and selects one for the beta.

## Candidate approaches

1. **Manual Anthropic Messages API typed-tool loop with a worker-side model proxy, running inside a per-job gVisor-backed `runsc` container.**
2. **Hosted Claude Code / `claude` CLI headless execution inside a container or microVM.**
3. **Firecracker microVM running a minimal Linux guest with the Agent SDK or a custom runtime.**
4. **General-purpose managed container service with seccomp/iptables hardening (e.g. gVisor on GKE, EKS/Fargate, or a self-managed `runsc` pool).**

The managed container option is treated as a deployment variant of option 1 rather than a distinct runtime model.

## Evaluation criteria

| Criterion | Why it matters for `post-call` |
|---|---|
| Multi-step agent fidelity | The runtime must preserve the `post-call` loop: read transcript, read prior context, reason, produce structured Markdown. |
| Full transcript and prior-context reads | The skill must read the entire transcript and cross-reference prior summaries, not sample or hallucinate. |
| Explicit tool mediation and auditability | Every file read, write, and network call must be named, allowlisted, and logged; no generic shell/browser. |
| Prompt-injection containment | A hostile transcript must not be able to exfiltrate credentials, escape the sandbox, or change tool permissions. |
| Job-level filesystem isolation | One job's files must not be visible to another; the sandbox must not see the host repo or customer data. |
| Deny-by-default network egress | Only explicit per-skill destinations can be reached; model API traffic must not leak the platform credential. |
| Model credential handling | The Anthropic API key must be unavailable to generated code and sandbox tools. |
| Cancellation, deadline expiry, and worker crashes | Immutable wall-clock deadlines from Slice 4 must terminate the sandbox; crashed workers must be recoverable. |
| Process termination guarantees | The sandbox process and any children must be killed when the job ends, times out, or is cancelled. |
| Ephemeral workspace destruction | Transient output directories, tmp files, and container layers must be removed after each attempt. |
| Local deterministic testing | Developers must be able to run the runtime contract and validation tests without a model key or cloud sandbox. |
| Deployment complexity | The beta must ship quickly and be operable by a small team. |
| Operational burden and observability | Logs, metrics, and failure classification must be available without persistent host state. |
| Expected beta cost | Per-job isolation overhead must fit a single-organization beta budget. |
| Portability and vendor lock-in | The contract should be provider-neutral enough to swap the sandbox backend later. |

## Option 1: Manual Anthropic Messages API typed-tool loop + gVisor-backed `runsc` container (recommended)

### 5B2B1 host privilege decision

The deployed worker uses a narrowly privileged root-owned Python broker rather
than gVisor rootless mode. The worker may sudo only to
`/usr/local/sbin/se-skills-runsc`; the broker accepts a strict frozen request on
stdin and authors the complete OCI bundle and config itself. Rootfs, process
argv/environment, UID/GID, capabilities, devices, namespaces, resource
limits, and mounts are broker-controlled; worker input is limited to the
container ID, sealed workspace paths, proxy socket, and typed job fields. This preserves
the OCI process UID 65532 without provisioning subuid/subgid ranges or
`newuidmap`/`newgidmap` setuid helpers. gVisor's rootless guide states that
`--rootless` maps only the caller UID and cannot map another user; the explicit
mapping path is therefore rejected for this host contract. `NoNewPrivileges`
is disabled only for this service-to-helper transition; the remaining systemd
hardening and `RestrictSUIDSGID=yes` remain in force.

The broker's root-owned configuration is rendered from the role's configured
runsc, rootfs, state, bundle, and staging paths, and preflight verifies the
helper/config/sudoers pair. It seals worker workspaces by no-follow validation
and atomic rename into a root-owned staging parent before binding them. It
accepts and forwards the worker's text-format list request without injecting
worker flags; list/delete verification and stale cleanup use this same boundary.
Requests are frozen operation-specific Pydantic models with `extra="forbid"`:
`run` carries the job and sealed workspace paths, `list`/`delete` carry a
state path, and `cleanup` carries only its minimum age. The broker owns the
complete OCI document, including the fixed image entrypoint/environment,
UID/GID, mounts, namespaces, capabilities, devices, read-only rootfs, and
deadline-derived CPU limit. The worker maps broker sentinels 65 (unverifiable
list output) and 66 (container still present after delete verification) to
closed cleanup failures.

The broker executes with `/opt/se-skills/venv/bin/python -I`. Isolated Python
mode prevents `PYTHONPATH` and user-site packages from changing root imports;
the venv's own packages remain available. Ansible owns the venv and broker
import-path directories as `root:root` and removes group/other write bits.
Preflight checks the interpreter, broker script, venv, and each configured
import-path directory before allowing the worker service to start.

**Vendor references (2026-08-11):**
- Anthropic Messages API reference: `https://docs.anthropic.com/en/api/messages`
- Anthropic Agent SDK overview (rejected as the runtime, but informative): `https://code.claude.com/docs/en/agent-sdk/overview`
- Anthropic Agent SDK Python reference: `https://code.claude.com/docs/en/agent-sdk/python`
- gVisor project and `runsc` OCI runtime: `https://gvisor.dev/docs/`
- gVisor security model: `https://gvisor.dev/docs/architecture_guide/security/`
- gVisor networking guide: `https://gvisor.dev/docs/user_guide/networking/`

**Versions recorded for this decision:**
- `httpx` 0.28.1 (sandbox-to-proxy HTTP transport)
- Anthropic Messages API `2023-06-01` (JSON request/response shape)
- Target model family: `claude-sonnet-4-6` (model identifier is configured by the worker and may change)
- gVisor `runsc` current stable as of 2026-08-11

### How it works

- The worker builds a `RuntimeJob` from the durable job row and the signed, read-only input manifest.
- The worker creates a per-attempt sandbox:
  - a temporary directory on the host for output,
  - a `runsc` container with a minimal Python image,
  - bind mounts for the transcript and any approved prior-context files (read-only),
  - a network namespace that can only reach the worker's model proxy.
- Inside the container, a small Python runtime runs a manual multi-step Anthropic Messages API loop with the `Allowlist` of typed tools.
- Tool implementations are typed and sandbox-aware: `read_transcript`, `read_prior_context`, `write_output`, `list_priors`, `finish`, `report_failure`. Generic `Bash`, `Git`, `Browser`, `Http`, `McpDiscover`, and `BypassPermissions` are not registered and cannot be invoked.
- Model calls do not leave the sandbox directly. The runtime POSTs to the worker's model proxy over a Unix domain socket (the sandbox runs with `--network=none`); the proxy validates a short-lived, job-scoped capability token, adds the platform Anthropic API key, and forwards the HTTPS call. The sandbox never sees the key.
- The runtime writes `output.md` and `sidecar.json` into the temporary output directory.
- The worker stops the container, validates the Markdown and sidecar with `output_schema.parse_output` outside the sandbox, and only then writes the validated artifacts to private org-scoped Storage and the `outputs` row.

A concrete feasibility harness is checked into `webapp/hosted/agent_loop_harness.py` and exercised by `eval/tests/test_agent_loop_harness.py`. It demonstrates the runtime starting with no Anthropic API key, routing model turns through a proxy, executing only allowlisted tools, writing output to the sandbox workspace, and honouring cancellation.

### Scoring

| Criterion | Score | Notes |
|---|---|---|
| Multi-step agent fidelity | Strong | A manual loop can implement the same read/reason/write cycle as any SDK. |
| Full transcript/prior reads | Strong | `read_transcript` and `read_prior_context` are explicit tools; the skill prompt requires full coverage. |
| Tool mediation and auditability | Strong | Every tool is declared in `Allowlist` and logged by the worker. |
| Prompt-injection containment | Strong | Untrusted content cannot invoke tools outside the allowlist or reach the network directly. |
| Filesystem isolation | Strong | gVisor Sentry intercepts syscalls; read-only bind mounts and job-scoped tmp directories enforce boundaries. |
| Deny-by-default egress | Strong | Only the model proxy is reachable; egress can be blocked by `runsc` network settings or an iptables sidecar. |
| Model credential handling | Strong | Key lives only in the worker/model proxy, not in sandbox memory. |
| Cancellation/deadline/crash | Strong | Worker cancels the container when `is_cancelled` fires or `execution_deadline` passes; lease recovery reclaims crashed attempts. |
| Process termination | Strong | Container stop removes the PID namespace and children. |
| Ephemeral workspace | Strong | Container layer and tmp directory are removed after the attempt. |
| Local testing | Strong | The contract and the `TypedToolRuntime` harness are tested with `httpx.MockTransport`; `runsc` can run locally on Linux. |
| Deployment complexity | Medium | `runsc` with Docker/Podman is well documented; a single beta worker can schedule containers. |
| Operational burden | Medium | Logs require container log shipping; gVisor adds a small Sentry overhead. |
| Expected beta cost | Medium-Low | Containers are lighter than microVMs; cost is dominated by model tokens. |
| Portability | Strong | The `SkillRuntime` protocol is provider-neutral; gVisor can be swapped for another OCI runtime or Firecracker. |

## Option 2: Hosted Claude Code / `claude` CLI headless

**Vendor reference (2026-08-11):**
- Claude Code CLI reference: `https://code.claude.com/docs/en/cli-reference`
- Claude Code permissions: `https://code.claude.com/docs/en/permissions`

### How it works

The worker shells out to `claude --bare -p <prompt>` inside a container, relying on `--allowedTools`, `--disallowedTools`, `--permission-mode`, and `--tools` flags to restrict the toolset.

### Scoring

| Criterion | Score | Notes |
|---|---|---|
| Multi-step agent fidelity | Strong | Claude Code is the reference implementation of the loop. |
| Tool mediation | Weak-Medium | `--allowedTools` auto-approves listed tools but does not remove them from context; `--tools` restricts the toolset but is coarse. Generic `Bash` cannot be fully eliminated without breaking the runtime. |
| Prompt-injection containment | Weak | The CLI bundles read/write/bash/browser tools and must be carefully locked down; a prompt injection can still ask for `Bash` and rely on permission-mode handling. |
| Filesystem isolation | Medium | Container boundaries help, but the CLI expects a working directory and can write anywhere the container user can. |
| Model credential handling | Weak | The CLI holds its own API key inside the sandbox process. |
| Auditability | Weak | Tool use is internal to the CLI session; fine-grained audit requires parsing CLI output. |
| Local testing | Medium | Requires `claude` binary and an API key for real runs; fakes cannot exercise the CLI path. |
| Deployment complexity | Medium | Packaging the CLI in a container is straightforward; scaling headless sessions is not. |
| Portability | Weak | Tightly coupled to Anthropic's CLI release cadence and permission model. |

### Why rejected

The CLI is designed for interactive developer use, not for an untrusted-input batch service. Its bundled toolset and permission model are too broad for a beta where transcript text is treated as hostile.

## Option 3: Anthropic Agent SDK (Python/TypeScript) with mediated typed tools

**Vendor references (2026-08-11):**
- Anthropic Agent SDK overview: `https://code.claude.com/docs/en/agent-sdk/overview`
- Anthropic Agent SDK Python reference: `https://code.claude.com/docs/en/agent-sdk/python`
- `claude-agent-sdk` PyPI: `https://pypi.org/project/claude-agent-sdk/`

### How it works

The runtime imports `claude-agent-sdk` and drives the Agent SDK loop with a restricted toolset. Anthropic documents the SDK as "Claude Code as a library" and bundles the `claude` CLI inside the package, so the SDK shares the same underlying process as the CLI.

### Scoring

| Criterion | Score | Notes |
|---|---|---|
| Multi-step agent fidelity | Strong | Same loop and tool model as Claude Code. |
| Tool mediation | Medium | The SDK exposes `tools`, `disallowed_tools`, `setting_sources`, `strict_mcp_config`, and permission mode, but it still relies on the bundled CLI process and its built-in tools. |
| Prompt-injection containment | Medium | Removing `Bash`, `Git`, `Browser`, and `Http` from the SDK context requires configuration that is not publicly documented as complete, and the SDK can inherit CLI permissions. |
| Model credential handling | Medium-Weak | The SDK/CLI process still needs an Anthropic API key inside the sandbox unless a custom transport is used; the public `transport` parameter is documented for SDK-to-CLI communication, not as a model-HTTP proxy. |
| Auditability | Medium | Tool calls are observable via the SDK API, but the loop is driven by the bundled CLI internals. |
| Local testing | Medium | Requires the `claude-agent-sdk` package and a fakeable transport; the bundled CLI binary complicates CI. |
| Portability | Medium | Tightly coupled to Anthropic's SDK/CLI release cadence. |

### Why rejected for the beta

The Agent SDK is documented as "Claude Code as a library" and bundles the same CLI that Option 2 rejects. Its `transport` parameter is for SDK-to-CLI communication, not a documented model-HTTP proxy, and its `ClaudeAgentOptions.env` is merged over the inherited process environment. Before selecting it we would need a reproducible demonstration that the bundled CLI can start with no real Anthropic key while every model request is routed through a worker proxy and every built-in tool is removed rather than merely disallowed. That demonstration is not available from the current public documentation. A manual Anthropic Messages API loop (Option 1) gives the same agent fidelity without the opaque CLI surface.

## Option 4: Firecracker microVM

**Vendor references (2026-08-11):**
- Firecracker design: `https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md`
- Firecracker jailer: `https://github.com/firecracker-microvm/firecracker/blob/main/docs/jailer.md`
- Production host setup: `https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md`

### How it works

Each attempt boots a Firecracker microVM with a minimal initramfs or rootfs containing the manual loop runtime. Network, block devices, and vCPUs are configured per job.

### Scoring

| Criterion | Score | Notes |
|---|---|---|
| Isolation | Strong | KVM-based microVMs offer VM-level boundaries. |
| Process termination | Strong | VM shutdown is a hard stop. |
| Filesystem isolation | Strong | Block devices can be per-VM and read-only. |
| Prompt-injection containment | Strong | Same as option 1 if the runtime is the same manual loop. |
| Local testing | Weak | Requires KVM and microVM image builds; hard to run in CI or on macOS. |
| Deployment complexity | High | Needs image pipeline, jailer configuration, cgroup setup, and orchestration. |
| Operational burden | High | MicroVM boot, logging, metrics, and debugging are heavier than containers. |
| Expected beta cost | Medium | microVMs are lightweight but still more overhead than containers for 5–10 minute jobs. |
| Portability | Medium | The runtime can be the same manual loop, but the orchestration layer is Firecracker-specific. |

### Why rejected for the beta

Firecracker is the strongest isolation choice, but the operational lift is disproportionate for the first single-org beta. The same manual loop runtime and trust-boundary design can be moved into Firecracker later without changing the worker contract.

## Option 5: Managed container service with hardening

This option runs the runtime inside a standard container on a managed service (e.g. GKE with gVisor, EKS Fargate, Fly Machines) and uses seccomp/iptables for hardening.

### Scoring

| Criterion | Score | Notes |
|---|---|---|
| Deployment complexity | Low | Managed services remove node management. |
| Operational burden | Low | Logging and scaling are handled by the platform. |
| Isolation | Medium-Weak | Standard containers share the host kernel; gVisor add-ons or dedicated VMs are needed for strong isolation. |
| Cost | Medium | Managed per-second billing is convenient but adds platform cost. |
| Local testing | Weak | Cannot easily reproduce the managed runtime locally. |

### Why rejected for the beta

A managed service without a gVisor or microVM layer does not provide the job-level filesystem and kernel isolation we need for untrusted transcript input. It may become the deployment target *for* option 1 later, but the runtime model itself should be container/gVisor-first.

## Decision

For the post-call runtime we will use **Option 1: a manual Anthropic Messages API typed-tool loop with a worker-side model proxy, running inside a per-job gVisor-backed `runsc` container.**

The core elements of this decision are operative for Slice 5B1 and Slice 5B2A.
Slice 5B2B1 now contains the pinned image/release tooling, root-owned evidence
manifest loading, canonical rootfs verification, durable state paths, Ubuntu
host contract, Ansible role, and deterministic/gated smoke entry points.
Applying the role to a real host and running the live Anthropic smoke remain
Slice 5B2B2.

This gives the highest agent fidelity with the smallest trusted surface area. The design is provider-neutral where possible and can be moved to Firecracker or a managed gVisor service without changing the worker contract.

## Trust boundary and data flow

```
SPA / API (app_user)
        |
        v
Postgres job queue  <-- signed tenant context, no transcript bodies
        |
        v
Worker (app_worker)  <-- holds model key, Storage signing secret
        |
        +-- resolves transcript and prior-context refs
        +-- materializes read-only files into job tmp dir
        +-- builds RuntimeJob + Allowlist
        |
        v
runsc container (SkillRuntime)
        |  - read-only transcript mount
        |  - read-only prior context mount
        |  - tmp output workspace
        |  - network only to worker model proxy
        |
        +-- manual Anthropic Messages API loop
        +-- typed tools only (read_transcript, write_output, ...)
        +-- model turn requests to worker model proxy
        |
        v
Worker model proxy  <-- adds Anthropic API key, forwards request
        |
        v
Anthropic API
        |
        v
runsc container writes output.md + sidecar.json
        |
        v
Worker validates Markdown and sidecar (output_schema.parse_output)
        |
        v
Storage (app_storage signed JWT) + outputs row (app_user via queue funcs)
```

## Explicitly resolved questions

- **Where the agent loop runs:** inside a per-attempt `runsc` container, started and monitored by the worker.
- **Where model calls originate:** from the worker's model proxy. The sandbox runtime POSTs a model turn to the proxy; the proxy attaches the platform API key and makes the outbound HTTPS call.
- **How the platform model credential remains unavailable:** it is never written into the container image, environment, or job payload. It lives only in the worker process and the proxy. The sandbox `TypedToolRuntime` sends no `x-api-key` header.
- **How transcript/reference inputs enter the sandbox:** the worker reads the authorized transcript and prior-context files using the `app_storage` role, then mounts them read-only into the container. No Storage credentials, signed URLs, or database credentials enter the sandbox.
- **How the output leaves the sandbox:** the runtime writes to a job-scoped temporary directory. The worker reads the files from outside the sandbox, validates them deterministically, and only then persists them.
- **How transcript/reference inputs are mapped to files:** `InputManifest` carries an explicit `transcript_ref` and a closed set of `prior_context_refs`. The runtime resolves only those filenames inside the read-only input workspace and never scans or reads unlisted files.
- **How tools are registered and allowlisted:** the worker passes an `Allowlist` of opaque tool names in the `RuntimeJob`. The runtime's closed tool registry rejects any tool not in the allowlist at dispatch, and each tool input is validated against a strict Pydantic model. Generic tools such as `Bash`, `Git`, `Browser`, `Http`, `McpDiscover`, and `BypassPermissions` are never allowed.
- **How network destinations are enforced:** `NetworkDestination` is a closed Pydantic model (`extra="forbid"`) that accepts only `http`/`https` hostnames. The container network namespace is restricted to the worker model proxy. Egress is deny-by-default; the proxy checks the request destination against a per-skill allowlist before forwarding.
- **How cancellation, deadline expiry, and worker crashes terminate or recover the sandbox:** `RuntimeJob` carries an immutable timezone-aware `execution_deadline` and a `CancellationToken` protocol. The harness races every in-flight model request against both cancellation and the deadline. The worker's `process_one` loop already monitors these signals. On expiry or cancellation, the worker stops the container. On worker crash, the Postgres lease expires and `recover_expired_leases` requeues the attempt.
- **Which component validates and persists the final artifact:** the worker validates `output.md` and `sidecar.json` with `output_schema.parse_output` outside the sandbox. Valid artifacts are written to private org-scoped Storage and recorded in the `outputs` table. Invalid artifacts are not persisted as Storage objects or `outputs` rows; the job is finalized with a fixed, redacted validation-failure category so invalid output is distinguishable from execution failure.

## Consequences

### Positive

- The trust boundary is small and explicit: the worker owns credentials, the sandbox owns only the current job's data.
- The `SkillRuntime` protocol and `RuntimeJob`/`RuntimeResult` contracts can be unit-tested with fakes today.
- The same design lifts cleanly into Firecracker or a managed gVisor service when the beta outgrows local containers.
- The manual loop avoids the opaque bundled CLI of the Agent SDK, making prompt-injection containment and tool mediation easier to reason about.

### Negative / risks

- gVisor adds a small syscall overhead and requires a Linux host with Docker or Podman.
- Running `runsc` in CI for integration tests may need privileged runners or a gVisor-enabled environment.
- The model proxy adds one network hop; it must be kept simple to avoid becoming a bottleneck or a new attack surface.
- The manual loop is more code to maintain than a high-level SDK, but it is also more auditable.

## Open Product Owner decisions

Core runtime decision accepted; the following are explicit Slice 5B2 operational follow-ups:

1. Which cloud host will run the worker containers (self-managed Linux VM vs managed Kubernetes vs managed container service)?
2. Do we want to prototype with Firecracker in parallel for a higher-isolation follow-up, or wait until after beta?
3. Which Anthropic model identifier is the beta default, and how is it rotated? The contract requires `requested_model` at construction; `claude-sonnet-4-6` is the current example in the harness and tests.
4. How is the gVisor/runsc image built, signed, and deployed, and who provisions the deny-by-default network namespace and model proxy?
