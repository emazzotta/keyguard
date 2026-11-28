"""Tests for caller source resolution (hostname / Docker container name)."""
from unittest.mock import MagicMock, patch

from keyguard_server.source import _resolve_hostname, resolve


def test_resolve_localhost():
    assert resolve("127.0.0.1") == "localhost"


def test_resolve_localhost_other_loopback():
    assert resolve("127.0.0.5") == "localhost"


def test_resolve_with_hint_uses_container_name():
    inspect = MagicMock(returncode=0, stdout="/my-app\n")
    with patch("subprocess.run", return_value=inspect):
        assert resolve("127.0.0.1", source_hint="abc123") == "my-app"


def test_resolve_with_hint_unresolvable_uses_raw_hint():
    inspect = MagicMock(returncode=1, stdout="")
    with patch("subprocess.run", return_value=inspect):
        assert resolve("127.0.0.1", source_hint="my-hostname") == "my-hostname"


def test_resolve_with_hint_non_localhost_includes_ip():
    inspect = MagicMock(returncode=0, stdout="/my-app\n")
    with patch("subprocess.run", return_value=inspect):
        assert resolve("172.17.0.2", source_hint="abc123") == "172.17.0.2 (my-app)"


def test_resolve_docker_ip_without_docker():
    with patch("subprocess.run", side_effect=FileNotFoundError), \
         patch("socket.gethostbyaddr", side_effect=OSError):
        assert resolve("172.17.0.2") == "172.17.0.2"


def test_resolve_docker_ip_with_match():
    docker_ps = MagicMock(returncode=0, stdout="abc123\n")
    docker_inspect = MagicMock(returncode=0, stdout="/my-container 172.17.0.2 \n")
    with patch("subprocess.run", side_effect=[docker_ps, docker_inspect]), \
         patch("socket.gethostbyaddr", side_effect=OSError):
        assert resolve("172.17.0.2") == "172.17.0.2 (my-container)"


def test_resolve_with_hostname_only():
    with patch("socket.gethostbyaddr", return_value=("macbook.local", [], [])), \
         patch("subprocess.run", side_effect=FileNotFoundError):
        assert resolve("192.168.1.50") == "192.168.1.50 (macbook.local)"


def test_resolve_with_hostname_and_container():
    docker_ps = MagicMock(returncode=0, stdout="abc123\n")
    docker_inspect = MagicMock(returncode=0, stdout="/my-app 172.17.0.2 \n")
    with patch("socket.gethostbyaddr", return_value=("some-host", [], [])), \
         patch("subprocess.run", side_effect=[docker_ps, docker_inspect]):
        assert resolve("172.17.0.2") == "172.17.0.2 (some-host, my-app)"


def test_resolve_deduplicates_hostname_and_container():
    docker_ps = MagicMock(returncode=0, stdout="abc123\n")
    docker_inspect = MagicMock(returncode=0, stdout="/my-app 172.17.0.2 \n")
    with patch("socket.gethostbyaddr", return_value=("my-app", [], [])), \
         patch("subprocess.run", side_effect=[docker_ps, docker_inspect]):
        assert resolve("172.17.0.2") == "172.17.0.2 (my-app)"


def test_resolve_hostname_returns_none_on_failure():
    with patch("socket.gethostbyaddr", side_effect=OSError):
        assert _resolve_hostname("10.0.0.5") is None


def test_resolve_hostname_ignores_ip_echo():
    with patch("socket.gethostbyaddr", return_value=("10.0.0.5", [], [])):
        assert _resolve_hostname("10.0.0.5") is None
