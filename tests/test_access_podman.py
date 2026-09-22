import os
import socket
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from sisyphus.capability import Capability
from sisyphus.evidence import Attempt
from sisyphus.network import Tunnel
from sisyphus.recipe import Recipe
from sisyphus.sandbox import Podman, Session

pytestmark = pytest.mark.podman


@pytest.fixture
def tool_image():
    image = os.environ.get("SISYPHUS_FETCH_TEST_IMAGE")
    if not image:
        pytest.skip("Set SISYPHUS_FETCH_TEST_IMAGE to a local image with python3 and git")
    return Podman().resolve(image)


def test_real_capability_is_explicit_and_absent_from_next_replay(project, tool_image):
    config = project(image=tool_image)
    attempt = Attempt(config)
    calls = []

    def handler(operation, arguments):
        calls.append((operation, arguments))
        if operation != "echo":
            raise ValueError("Operation not granted")
        return arguments

    with Capability("board", handler) as capability:
        directory = capability.directory
        recipe = Recipe(attempt, "test_capability")
        result = recipe.run(
            [
                "python3",
                "-c",
                'import sys; sys.path.insert(0,"/sisyphus"); from sisyphus_device import call; '
                'assert call("board","echo",message="hello") == {"message":"hello"}; '
                'print("capability-ok")',
            ],
            access=[capability],
        )
        assert result.stdout.strip() == "capability-ok"
        assert calls == [("echo", {"message": "hello"})]
        isolated = recipe.run(
            [
                "python3",
                "-c",
                "import os; from pathlib import Path; "
                'assert "SISYPHUS_CAPABILITIES" not in os.environ; '
                'assert not Path("/capabilities").exists(); print("isolated")',
            ]
        )
        assert isolated.stdout.strip() == "isolated"
    assert not directory.exists()


def test_real_offline_container_has_only_the_granted_proxy_route(tool_image, tmp_path, monkeypatch):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(self.request.recv(100))

    echo = socketserver.TCPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=echo.serve_forever, daemon=True)
    thread.start()
    destinations = []

    def connect(host, timeout):
        destinations.append(host)
        return socket.create_connection(echo.server_address, timeout=timeout)

    monkeypatch.setattr("sisyphus.network.public_socket", connect)
    try:
        with tempfile.TemporaryDirectory(prefix="sisyphus-proxy-test-") as temporary:
            broker = Path(temporary)
            with Tunnel(broker / "socket", ["approved.test"], limit=4096, timeout=15):
                work = tmp_path / "work"
                work.mkdir()
                session = Session(Podman(), tool_image, work, {})
                session.extra_mounts = [(broker, "/broker", True)]
                script = """import socket
from pathlib import Path
assert sorted(p.name for p in Path("/sys/class/net").iterdir()) == ["lo"]
with socket.socket(socket.AF_UNIX) as s:
    s.connect("/broker/socket")
    s.sendall(b"CONNECT denied.test:443 HTTP/1.1\\r\\n\\r\\n")
    assert b"403" in s.recv(1000)
with socket.socket(socket.AF_UNIX) as s:
    s.connect("/broker/socket")
    s.sendall(b"CONNECT approved.test:443 HTTP/1.1\\r\\n\\r\\n")
    assert b"200" in s.recv(1000)
    s.sendall(b"payload")
    assert s.recv(1000) == b"payload"
print("proxy-ok")
"""
                logs = tmp_path / "logs"
                logs.mkdir()
                with session:
                    result = session.execute(("python3", "-c", script), {}, logs, 15)
                assert result.returncode == 0, (logs / "stderr.log").read_text()
                assert (logs / "stdout.log").read_text().strip() == "proxy-ok"
                assert destinations == ["approved.test"]
    finally:
        echo.shutdown()
        echo.server_close()
        thread.join()


def test_git_checkout_budget_stops_the_entire_writer_group(tool_image, tmp_path):
    import json

    import sisyphus

    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "url": "https://unused.test/repo.git",
                "ref": "main",
                "max_bytes": 1024**2,
                "max_files": 100,
            }
        )
    )
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /recipe/repo\n"
        "for name in a b c; do\n"
        "  dd if=/dev/zero of=/recipe/repo/$name bs=524288 count=1 2>/dev/null\n"
        "done\nsleep 2\ntouch /recipe/late-writer\nsleep 30\n"
    )
    fake_git.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    session = Session(Podman(), tool_image, work, {})
    session.extra_mounts = [
        (request, "/request.json", True),
        (fake_git, "/usr/bin/git", True),
        (Path(sisyphus.__file__).parent / "agent", "/editor", True),
    ]
    with session:
        result = session.execute(("python3", "/editor/fetch_git.py"), {}, logs, 8)
        assert result.returncode != 0
        assert not result.timed_out, (logs / "stderr.log").read_text()
        # A surviving Git child would create the marker after the helper's budget exit.
        wait_logs = tmp_path / "wait-logs"
        wait_logs.mkdir()
        session.execute(("sleep", "2"), {}, wait_logs, 4)
        assert not (work / "late-writer").exists()
