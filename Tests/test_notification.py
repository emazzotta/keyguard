"""Tests for the osascript notification helper."""
from keyguard_server.notification import escape_osascript


def test_escape_osascript_handles_quotes():
    assert escape_osascript('key "test"') == 'key \\"test\\"'


def test_escape_osascript_handles_backslash():
    assert escape_osascript("path\\file") == "path\\\\file"


def test_escape_osascript_escapes_newlines():
    """Unescaped \\n in an AppleScript string literal terminates the string and breaks osascript."""
    assert escape_osascript("line1\nline2") == "line1\\nline2"


def test_escape_osascript_escapes_carriage_returns():
    assert escape_osascript("a\rb") == "a\\rb"


def test_escape_osascript_combined():
    assert escape_osascript('a\nb"c\\d') == 'a\\nb\\"c\\\\d'
