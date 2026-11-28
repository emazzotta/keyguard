"""Resolves a caller's IP into a friendly name (hostname / Docker container)."""
from __future__ import annotations

import socket
import subprocess


def _resolve_hostname(ip: str) -> str | None:
    if ip.startswith("127."):
        return "localhost"
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
    except (socket.herror, socket.gaierror, OSError):
        return None
    if hostname and hostname != ip:
        return hostname
    return None


def _docker_container_for_ip(ip: str) -> str | None:
    try:
        ps = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        if ps.returncode != 0 or not ps.stdout.strip():
            return None
        container_ids = ps.stdout.strip().split("\n")
        inspect = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"]
            + container_ids,
            capture_output=True, text=True, timeout=5,
        )
        if inspect.returncode != 0:
            return None
        for line in inspect.stdout.strip().split("\n"):
            parts = line.strip().split()
            if len(parts) >= 2 and ip in parts[1:]:
                return parts[0].lstrip("/")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _docker_container_for_hint(hint: str) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Name}}", hint],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().lstrip("/")
    return None


def resolve(ip: str, source_hint: str | None = None) -> str:
    if source_hint:
        container_name = _docker_container_for_hint(source_hint)
        name = container_name or source_hint
        if ip.startswith("127."):
            return name
        return f"{ip} ({name})"
    if ip.startswith("127."):
        return "localhost"
    names: list[str] = []
    hostname = _resolve_hostname(ip)
    if hostname:
        names.append(hostname)
    container = _docker_container_for_ip(ip)
    if container and container not in names:
        names.append(container)
    if names:
        return f"{ip} ({', '.join(names)})"
    return ip
