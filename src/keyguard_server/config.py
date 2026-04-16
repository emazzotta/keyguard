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
BRIDGE_TOKEN_KEYGUARD_KEY: Final = "MAC_BRIDGE_TOKEN"
BRIDGE_TOKEN_RETRY_COOLDOWN: Final = 60.0
MAX_BRIDGE_OUTPUT_BYTES: Final = 1_048_576
