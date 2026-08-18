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


@dataclass(frozen=True)
class ParsedFirewallPolicy:
    rules: tuple[FirewallRule, ...]
    input_policy: str | None
    output_policy: str | None
    valid: bool


_RULE = re.compile(
    r"^\s*(?:(?:meta skuid (?P<uid>\d+)\s+)?"
    r"(?:(?P<family>ip6?) daddr (?P<destination>\S+)\s+)?"
    r"(?:(?P<protocol>tcp|udp) dport (?P<port>\d+)\s+)?"
    r"(?P<action>accept|drop)\s*)$"
)
_CHAIN = re.compile(r"^\s*chain (?P<name>input|output) \{$")
_POLICY = re.compile(r"policy (?P<policy>accept|drop);")
_KNOWN_BASE = re.compile(
    r'^\s*(?:iifname "lo"|oifname "lo"|ct state established,related)'
    r'(?:\s+accept)?$|^\s*ip saddr \S+ tcp dport \d+ accept$'
)


def parse_output_policy(text: str) -> ParsedFirewallPolicy:
    """Parse the live table and reject unknown rules or chain policies."""
    rules: list[FirewallRule] = []
    chain: str | None = None
    input_policy: str | None = None
    output_policy: str | None = None
    valid = True
    for line in text.splitlines():
        stripped = line.strip()
        chain_match = _CHAIN.match(line)
        if chain_match:
            chain = chain_match.group("name")
            continue
        if chain is not None and stripped == "}":
            chain = None
            continue
        if chain is None:
            continue
        if stripped.startswith("type "):
            policy_match = _POLICY.search(stripped)
            if policy_match:
                if chain == "input":
                    input_policy = policy_match.group("policy")
                else:
                    output_policy = policy_match.group("policy")
            continue
        if not stripped or stripped.startswith("comment "):
            continue
        if _KNOWN_BASE.fullmatch(stripped):
            continue
        if chain != "output":
            valid = False
            continue
        match = _RULE.match(line)
        if match is None:
            valid = False
            continue
        destination = match.group("destination")
        try:
            parsed_destination = (
                ipaddress.ip_network(destination, strict=False)
                if destination
                else None
            )
        except ValueError:
            valid = False
            continue
        rules.append(
            FirewallRule(
                action=match.group("action"),
                destination=parsed_destination,
                uid=int(match.group("uid")) if match.group("uid") else None,
                protocol=match.group("protocol"),
                port=int(match.group("port")) if match.group("port") else None,
            )
        )
    if (
        input_policy is None
        or output_policy is None
        or input_policy != "drop"
        or output_policy != "drop"
    ):
        valid = False
    return ParsedFirewallPolicy(tuple(rules), input_policy, output_policy, valid)


def evaluate_output_policy(
    rules: ParsedFirewallPolicy | tuple[FirewallRule, ...],
    uid: int,
    destination: str,
    protocol: str,
    port: int,
) -> str:
    """Evaluate one egress tuple in nft rule order, defaulting to drop."""
    address = ipaddress.ip_address(destination)
    ordered = rules.rules if isinstance(rules, ParsedFirewallPolicy) else rules
    for rule in ordered:
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
