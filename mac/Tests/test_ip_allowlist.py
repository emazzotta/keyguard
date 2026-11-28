"""Tests for the IP allowlist."""
import pytest

from keyguard_server.ip_allowlist import is_allowed


@pytest.mark.parametrize("ip,expected", [
    ("127.0.0.1", True),
    ("127.255.255.255", True),
    ("172.16.0.1", True),
    ("172.17.0.1", True),
    ("172.31.255.255", True),
    ("192.168.65.1", True),
    ("192.168.65.255", True),
    ("172.32.0.1", False),
    ("192.168.1.1", False),
    ("10.0.0.1", False),
    ("8.8.8.8", False),
    ("not-an-ip", False),
])
def test_is_allowed(ip: str, expected: bool) -> None:
    assert is_allowed(ip) == expected
