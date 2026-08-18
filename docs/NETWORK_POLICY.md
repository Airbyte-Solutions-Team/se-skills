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
Direct Anthropic egress is disabled. The worker uses a controlled forward proxy
at `ANTHROPIC_EGRESS_PROXY_URL`; set `HOSTED_ANTHROPIC_PROXY_HOST` and
`HOSTED_ANTHROPIC_PROXY_PORT` to the same endpoint so preflight verifies the
live accept. The firewall allows only the proxy's configured IPv4 address and
port. IPv6 proxy addresses are rejected by the host contract. If that address is RFC1918, the policy places a
narrow worker-UID-scoped exception after metadata/link-local drops and before
generic private-range drops. The proxy must allow only `CONNECT`/TLS to
`api.anthropic.com:443`, resolve DNS itself, authenticate the worker, enforce
bounded timeouts and concurrency, redact audit logs, and never log request or
response bodies. The configured URL carries no credentials; authentication is
provided by operator-owned network identity such as mTLS or an equivalent
mechanism. This authentication direction is decided but is not implemented in
the worker yet: there is no client-certificate or trust-bundle wiring and no
live handshake test. Credentials stay out of the proxy URL. Provisioning,
trust-bundle/client-certificate wiring, and rotation remain 5B2B2 work.

`HOSTED_APPROVED_HTTPS_DESTINATIONS` is the preflight-verified set of approved
HTTPS destinations. It must contain the same destinations that the rendered
policy accepts for the worker UID on TCP/443; the proxy endpoint is verified
separately. An empty or incoherent set fails closed.
The Product Owner rejected maintained Anthropic CIDRs in favor of this proxy
contract. Provisioning and operating the actual proxy remains 5B2B2 work.
TLS hostname verification and worker-side proxy mediation remain primary
controls; the host firewall is defense in depth.

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
port. It also requires `policy drop` on both input and output chains and
rejects any unknown rule or verdict rather than silently ignoring it. Missing
`nft`, a missing live table, an empty approved HTTPS destination set, or an
unparseable chain fails closed; rendering the template alone is not evidence
that the live policy is loaded.

## Failure expectations

Unexpected inbound traffic, sandbox network access, missing proxy mediation,
unapproved destinations, malformed capability requests, and unknown host
contract facts fail closed. A successful firewall template render is not proof
that a live host is correctly configured; that requires the gated 5B2B2 smoke.
