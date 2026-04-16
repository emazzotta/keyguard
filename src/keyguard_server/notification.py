"""macOS notifications via osascript - fired async to keep response paths fast."""
from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timezone

from . import source as source_resolver


def escape_osascript(s: str) -> str:
    """Escape backslash, quote, and control characters that would break the AppleScript literal."""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
    )


def _send(keys: list[str], client_ip: str, cached: bool, source_hint: str | None) -> None:
    try:
        source = source_resolver.resolve(client_ip, source_hint)
        cache_hint = " (cached)" if cached else ""
        key_list = ", ".join(keys)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = escape_osascript(f"{timestamp} - {key_list} read by {source}{cache_hint}")
        result = subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "keyguard"'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            print(f"[keyguard] notification failed (rc={result.returncode}): {result.stderr.strip()}",
                  file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[keyguard] notification timed out", file=sys.stderr)
    except FileNotFoundError:
        print("[keyguard] osascript not found", file=sys.stderr)
    except Exception as e:
        print(f"[keyguard] notification error: {e}", file=sys.stderr)


def notify_async(keys: list[str], client_ip: str, cached: bool,
                 source_hint: str | None = None) -> None:
    threading.Thread(
        target=_send, args=(keys, client_ip, cached, source_hint),
        daemon=True,
    ).start()
