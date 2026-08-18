# Hosted worker network policy

The system has three separate boundaries. A control that belongs to one
boundary must not be treated as proof for another.

## Untrusted sandbox

The sandbox runs one attempt in an ephemeral gVisor `runsc` container with
`--network=none`. It receives only the worker-materialized, manifest-listed
input files and its output workspace. Model requests use the worker-created
per-attempt Unix socket. The sandbox has no database credentials, Storage
credentials, model key, host environment, unrestricted shell, browser, Git, or
arbitrary network.

The worker-side proxy validates the capability, sequence, route, model,
deadline, request shape, and per-skill destination policy before forwarding a
model request.

## Trusted worker

The worker resolves inputs and persists outputs. Its outbound network is
controlled by the host's `inet se_skills` nftables table and by application
protocol checks. The shipped policy is deny-by-default, drops cloud metadata,
link-local, RFC1918, and IPv6 private ranges before any accept, and permits
only explicitly configured dependency IP/CIDR rules combined with
`meta skuid 995`.

The firewall does not use `flush ruleset`; it replaces only the `se_skills`
table so it does not destroy provider or container-manager tables. DNS and NTP
allowlists are empty by default. Destination variables must be IP addresses or
CIDRs; hostnames such as `ghcr.io` are not inserted into `ip daddr` rules.

Static IP allowlisting is not a safe primary control for changing upstream
services. TLS certificate/hostname verification and worker-side proxy
mediation are primary controls; nftables is defense in depth. Provider IP
resolution and the final dependency list are deferred to 5B2B2.

## API service

FastAPI is a separate trusted service boundary. It authenticates members,
enforces organization authorization, signs short-lived Storage requests, and
enqueues jobs. The worker receives queue-function access rather than broad
tenant-table access. The API does not expose a public application port on the
worker host.

Production preflight obtains `nft list table inet se_skills` through its host
probe and evaluates ordered rules for identity, destination, protocol, and
port. Missing `nft` or a missing live table fails closed; rendering the
template alone is not evidence that the live policy is loaded.

## Failure expectations

Unexpected inbound traffic, sandbox network access, missing proxy mediation,
unapproved destinations, malformed capability requests, and unknown host
contract facts fail closed. A successful firewall template render is not proof
that a live host is correctly configured; that requires the gated 5B2B2 smoke.
