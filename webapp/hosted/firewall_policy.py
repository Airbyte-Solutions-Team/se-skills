"""Parse and evaluate the hosted worker's ordered nftables output policy."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FirewallRule:
    action: str
    destination: ipaddress._BaseNetwork | None = None
    uid: int | None = None
    protocol: str | None = None
    port: int | None = None


_RULE = re.compile(
    r"^\s*(?:(?:meta skuid (?P<uid>\d+)\s+)?"
    r"(?:(?P<family>ip6?) daddr (?P<destination>\S+)\s+)?"
    r"(?:(?P<protocol>tcp|udp) dport (?P<port>\d+)\s+)?"
    r"(?P<action>accept|drop)\s*)$"
)


def parse_output_policy(text: str) -> tuple[FirewallRule, ...]:
    """Parse ordered address, identity, protocol, and action rules."""
    rules: list[FirewallRule] = []
    in_output = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("chain output"):
            in_output = True
            continue
        if in_output and stripped == "}":
            break
        if not in_output:
            continue
        match = _RULE.match(line)
        if match is None:
            continue
        destination = match.group("destination")
        rules.append(
            FirewallRule(
                action=match.group("action"),
                destination=(
                    ipaddress.ip_network(destination, strict=False)
                    if destination
                    else None
                ),
                uid=int(match.group("uid")) if match.group("uid") else None,
                protocol=match.group("protocol"),
                port=int(match.group("port")) if match.group("port") else None,
            )
        )
    return tuple(rules)


def evaluate_output_policy(
    rules: tuple[FirewallRule, ...],
    uid: int,
    destination: str,
    protocol: str,
    port: int,
) -> str:
    """Evaluate one egress tuple in nft rule order, defaulting to drop."""
    address = ipaddress.ip_address(destination)
    for rule in rules:
        if rule.uid is not None and rule.uid != uid:
            continue
        if rule.destination is not None and address not in rule.destination:
            continue
        if rule.protocol is not None and rule.protocol != protocol:
            continue
        if rule.port is not None and rule.port != port:
            continue
        return rule.action
    return "drop"
