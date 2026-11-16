#!/usr/bin/env python3
import os
import sys
from pathlib import Path

client = Path(__file__).resolve().parent / "keyguard-passkey.pyz"
os.execv(sys.executable, [sys.executable, str(client), "pending", *sys.argv[1:]])
