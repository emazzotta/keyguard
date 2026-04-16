"""In-memory TTL cache for decrypted secrets, scoped per client IP."""
from __future__ import annotations

import threading
import time

_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _cache_key(ip: str, name: str) -> tuple[str, str]:
    return (ip, name)


def get(ip: str, key: str) -> str | None:
    with _cache_lock:
        ck = _cache_key(ip, key)
        entry = _cache.get(ck)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _cache[ck]
        return None


def put(ip: str, key: str, value: str, timeout: int) -> None:
    with _cache_lock:
        _cache[_cache_key(ip, key)] = (value, time.monotonic() + timeout)


def clear() -> None:
    with _cache_lock:
        _cache.clear()


def get_shared(lookup_ips: list[str], key: str) -> str | None:
    if "*" in lookup_ips:
        with _cache_lock:
            for (_ip, name), (value, expiry) in list(_cache.items()):
                if name == key and expiry > time.monotonic():
                    return value
        return None
    for ip in lookup_ips:
        val = get(ip, key)
        if val is not None:
            return val
    return None


def parse_share(query: dict[str, list[str]], client_ip: str) -> list[str]:
    share_values = query.get("share")
    if not share_values:
        return [client_ip]
    raw = share_values[0].strip()
    if raw == "all":
        return ["*"]
    # Strip "*" from user input - it is the internal wildcard sentinel and
    # must only be settable via the documented "share=all" opt-in, never by
    # smuggling a literal "*" into the IP list.
    ips = [s.strip() for s in raw.split(",") if s.strip() and s.strip() != "*"]
    if client_ip not in ips:
        ips.append(client_ip)
    return ips
