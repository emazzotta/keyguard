"""Server-wide constants and environment-driven paths."""
from __future__ import annotations

import os
from ipaddress import IPv4Network
from pathlib import Path
from typing import Final

KEYGUARD_BIN: Final = Path("/usr/local/bin/keyguard")
HOST: Final = "0.0.0.0"
PORT: Final = 7777
SUBPROCESS_TIMEOUT: Final = 60
MAX_SECRET_BYTES: Final = 65_536
MAX_CACHE_TIMEOUT: Final = 300

ALLOWED_NETWORKS: Final = (
    IPv4Network("127.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.65.0/24"),
)

BRIDGE_CONFIG_PATH: Final = Path(
    os.environ.get("KEYGUARD_BRIDGE_CONFIG_FILE")
    or "~/.mac-bridge-endpoints.yaml"
).expanduser()
MAX_BRIDGE_OUTPUT_BYTES: Final = 1_048_576


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name, "")
    return Path(value).expanduser() if value else None


# The phone fallback (phone.py): off unless both files exist.
PHONE_CLIENT: Final = _path_from_env("KEYGUARD_PHONE_CLIENT")
PHONE_SIGNING_KEY: Final = _path_from_env("KEYGUARD_PHONE_SIGNING_KEY")
PHONE_HOST: Final = "mac"
PHONE_DELAY: Final = 15
# The client's 10 s request plus its 300 s wait for an answer, with a margin.
PHONE_WAIT: Final = 330
# The client checks for SIGTERM between its 2 s polls, then withdraws its request.
PHONE_STOP_GRACE: Final = 5
PHONE_PATH: Final = "/opt/homebrew/opt/openssl@3/bin:/opt/homebrew/bin:/usr/bin:/bin"
