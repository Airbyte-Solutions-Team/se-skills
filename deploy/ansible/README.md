# Hosted worker Ansible package

This package configures a dedicated Ubuntu 24.04 x86_64 hosted worker. It reads
the authoritative values from `../pins.json`, installs `runsc` with its pinned
SHA-512 checksum, and configures a hardened systemd service and deny-by-default
nftables policy.

Review the generated plan without changing a host:

```bash
ansible-playbook -i inventory.example.ini site.yml --check --diff
```

Applying this package to a real host is deferred to Slice 5B2B2. The referenced
`/etc/se-skills/worker.env` must be provisioned by an approved secret-management
process; this role never templates secret values.
