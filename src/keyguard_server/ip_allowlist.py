"""IP allowlist - rejects traffic from outside loopback and Docker subnets."""
from __future__ import annotations

import ipaddress
from ipaddress import IPv4Address

from .config import ALLOWED_NETWORKS


def is_allowed(client_ip: str) -> bool:
    try:
        addr = IPv4Address(client_ip)
    except ipaddress.AddressValueError:
        return False
    return any(addr in network for network in ALLOWED_NETWORKS)
