"""Unit tests for the wire-format encoding helpers."""
import base64

from keyguard_server.encoding import (
    decode_value,
    encode_value,
    format_response,
    parse_key_value_output,
    parse_timeout,
)


def test_decode_value_plain():
    assert decode_value("hello") == "hello"


def test_decode_value_encoded():
    encoded = base64.b64encode(b'{"key": "val"}').decode()
    assert decode_value(f"base64:{encoded}") == '{"key": "val"}'


def test_decode_value_invalid_base64_returns_unchanged():
    assert decode_value("base64:!!!") == "base64:!!!"


def test_encode_value_singleline_unchanged():
    assert encode_value("hello") == "hello"


def test_encode_value_multiline_uses_base64():
    result = encode_value("line1\nline2")
    assert result.startswith("base64:")
    assert decode_value(result) == "line1\nline2"


def test_format_response_single_key_returns_raw():
    assert format_response(["TOKEN"], {"TOKEN": "secret"}) == "secret"


def test_format_response_single_key_multiline_unchanged():
    assert format_response(["K"], {"K": "line1\nline2"}) == "line1\nline2"


def test_format_response_multiple_keys_kv_lines():
    result = format_response(["A", "B"], {"A": "1", "B": "2"})
    assert result == "A=1\nB=2\n"


def test_format_response_multiple_keys_with_multiline_uses_base64():
    result = format_response(["A", "B"], {"A": "multi\nline", "B": "plain"})
    lines = result.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("A=base64:")
    assert lines[1] == "B=plain"


def test_parse_key_value_output_decodes_base64():
    encoded = base64.b64encode(b'{\n  "type": "sa"\n}').decode()
    output = f"JSON_KEY=base64:{encoded}\nPLAIN=hello"
    result = parse_key_value_output(output)
    assert result["JSON_KEY"] == '{\n  "type": "sa"\n}'
    assert result["PLAIN"] == "hello"


def test_parse_timeout_valid():
    assert parse_timeout({"timeout": ["30"]}, cap=300) == 30


def test_parse_timeout_caps_at_max():
    assert parse_timeout({"timeout": ["9999"]}, cap=300) == 300


def test_parse_timeout_zero_returns_none():
    assert parse_timeout({"timeout": ["0"]}, cap=300) is None


def test_parse_timeout_negative_returns_none():
    assert parse_timeout({"timeout": ["-1"]}, cap=300) is None


def test_parse_timeout_missing_returns_none():
    assert parse_timeout({}, cap=300) is None


def test_parse_timeout_invalid_returns_none():
    assert parse_timeout({"timeout": ["abc"]}, cap=300) is None
