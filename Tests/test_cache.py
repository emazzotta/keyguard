"""Unit tests for the in-memory TTL cache."""
import time

from keyguard_server import cache


def test_put_and_get_returns_value():
    cache.put("10.0.0.1", "K", "V", 10)
    assert cache.get("10.0.0.1", "K") == "V"


def test_get_scoped_to_ip():
    cache.put("10.0.0.1", "K", "V", 10)
    assert cache.get("10.0.0.2", "K") is None


def test_expired_entry_returns_none():
    cache.put("10.0.0.1", "K", "V", 0)
    time.sleep(0.01)
    assert cache.get("10.0.0.1", "K") is None


def test_clear_wipes_everything():
    cache.put("10.0.0.1", "A", "1", 60)
    cache.put("10.0.0.2", "B", "2", 60)
    cache.clear()
    assert cache.get("10.0.0.1", "A") is None
    assert cache.get("10.0.0.2", "B") is None


def test_get_shared_with_wildcard():
    cache.put("10.0.0.1", "K", "V", 10)
    assert cache.get_shared(["*"], "K") == "V"


def test_get_shared_with_specific_ip_list():
    cache.put("10.0.0.1", "K", "V", 10)
    assert cache.get_shared(["10.0.0.1", "10.0.0.2"], "K") == "V"
    assert cache.get_shared(["10.0.0.3"], "K") is None


def test_parse_share_defaults_to_client_ip():
    assert cache.parse_share({}, "10.0.0.1") == ["10.0.0.1"]


def test_parse_share_all_returns_wildcard():
    assert cache.parse_share({"share": ["all"]}, "10.0.0.1") == ["*"]


def test_parse_share_explicit_list_includes_client():
    result = cache.parse_share({"share": ["10.0.0.2,10.0.0.3"]}, "10.0.0.1")
    assert set(result) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}


def test_parse_share_does_not_duplicate_client():
    result = cache.parse_share({"share": ["10.0.0.1,10.0.0.2"]}, "10.0.0.1")
    assert result.count("10.0.0.1") == 1


def test_parse_share_strips_wildcard_from_user_input():
    """Security: caller cannot smuggle '*' into the share list to bypass IP isolation."""
    result = cache.parse_share({"share": ["10.0.0.2,*,10.0.0.3"]}, "10.0.0.1")
    assert "*" not in result
    assert set(result) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}


def test_parse_share_only_wildcard_does_not_match_anywhere():
    result = cache.parse_share({"share": ["*"]}, "10.0.0.1")
    assert result == ["10.0.0.1"]
    cache.put("10.0.0.99", "K", "leaked", 60)
    assert cache.get_shared(result, "K") is None
