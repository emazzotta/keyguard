"""The launcher starts wherever Python 3 does: as python3, or as python where python3 is the Microsoft Store stub."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "keyguard-passkey"
STORE_STUB = "#!/bin/sh\necho 'Python was not found; run without arguments to install from the Microsoft Store' >&2\nexit 9009\n"
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs the launcher through /bin/sh")


def link_python(folder: Path, name: str) -> None:
    (folder / name).symlink_to(sys.executable)


def launch(folder: Path, **environ: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(LAUNCHER), "version"], capture_output=True, text=True, timeout=30, check=False,
                          env={"PATH": str(folder), "HOME": str(folder), **environ})


@pytest.mark.spec("README.md")
@pytest.mark.parametrize("name", ["python3", "python"])
def test_should_start_under_either_name_of_python(tmp_path, name):
    link_python(tmp_path, name)

    started = launch(tmp_path)

    assert (started.returncode, started.stdout.count(".")) == (0, 2)


@pytest.mark.spec("README.md")
def test_should_prefer_python_over_the_store_stub_on_windows(tmp_path):
    link_python(tmp_path, "python")
    stub = tmp_path / "python3"
    stub.write_text(STORE_STUB, encoding="utf-8")
    stub.chmod(0o755)

    assert launch(tmp_path, OS="Windows_NT").returncode == 0


@pytest.mark.spec("README.md")
def test_should_name_the_missing_interpreter(tmp_path):
    started = launch(tmp_path)

    assert started.returncode == 127
    assert "needs Python 3 on PATH" in started.stderr
