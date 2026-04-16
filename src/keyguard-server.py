#!/usr/bin/env python3
"""Entrypoint shim - the launchd plist runs this file. Real code lives in keyguard_server/."""
from __future__ import annotations

import sys
from pathlib import Path

# When installed at /usr/local/lib/keyguard/keyguard-server.py, the package sits at
# /usr/local/lib/keyguard/keyguard_server/. Add the parent dir to sys.path so the
# package import works regardless of how the script is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from keyguard_server.server import main  # noqa: E402

if __name__ == "__main__":
    main()
