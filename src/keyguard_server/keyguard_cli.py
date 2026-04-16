"""Wrapper around the keyguard CLI binary - one place that calls subprocess."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .config import KEYGUARD_BIN, SUBPROCESS_TIMEOUT


@dataclass(frozen=True)
class CliResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.not_found

    @property
    def touch_id_cancelled(self) -> bool:
        return self.rc == 2


def _run(args: list[str], stdin_value: str | None = None) -> CliResult:
    try:
        result = subprocess.run(
            [str(KEYGUARD_BIN)] + args,
            capture_output=True, text=True, errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            input=stdin_value,
        )
    except subprocess.TimeoutExpired:
        return CliResult(rc=-1, stdout="", stderr="keyguard timed out", timed_out=True)
    except FileNotFoundError:
        return CliResult(rc=-1, stdout="", stderr="keyguard binary not found", not_found=True)
    except OSError as e:
        return CliResult(rc=-1, stdout="", stderr=f"keyguard failed to start: {e}", not_found=True)
    return CliResult(rc=result.returncode, stdout=result.stdout, stderr=result.stderr)


def get(*keys: str, cache_duration: int | None = None) -> CliResult:
    args = ["get", *keys]
    if cache_duration:
        args += ["--cache-duration", str(cache_duration)]
    return _run(args)


def list_keys(cache_duration: int | None = None) -> CliResult:
    args = ["list"]
    if cache_duration:
        args += ["--cache-duration", str(cache_duration)]
    return _run(args)


def set_secret(name: str, value: str) -> CliResult:
    return _run(["set", name], stdin_value=value)
