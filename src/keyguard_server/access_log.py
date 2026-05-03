"""Access log for secret reads and bridge calls. Appends a line per event - no UI."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import source as source_resolver

LOG_PATH: Path = Path(
    os.environ.get("KEYGUARD_LOG_FILE") or "~/.keyguard/access.log"
).expanduser()


def _format_line(keys: list[str], client_ip: str, cached: bool,
                 source_hint: str | None) -> str:
    source = source_resolver.resolve(client_ip, source_hint)
    cache_hint = " (cached)" if cached else ""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{timestamp} {source} {', '.join(keys)}{cache_hint}\n"


def log_access(keys: list[str], client_ip: str, cached: bool,
               source_hint: str | None = None) -> None:
    """Append one line per access event. Failures are logged to stderr but never raise."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = _format_line(keys, client_ip, cached, source_hint)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        print(f"[keyguard] access log write failed: {e}", file=sys.stderr)
