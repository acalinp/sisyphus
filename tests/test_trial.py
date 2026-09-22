import subprocess
import sys
import time

from sisyphus.solver import pytest_environment


def test_pytest_worker_exits_when_parent_pipe_closes(tmp_path):
    marker = tmp_path / "started"
    test = tmp_path / "test_wait.py"
    test.write_text(
        "import time\nfrom pathlib import Path\n"
        f"def test_wait():\n    Path({str(marker)!r}).write_text('started')\n"
        "    time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "sisyphus.trial", "-q", str(test)],
        cwd=tmp_path,
        env=pytest_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        process.stdin.close()
        process.wait(timeout=5)
        assert process.returncode != 0
        assert b"Fatal Python error" not in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
