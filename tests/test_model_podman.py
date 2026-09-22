"""Real containers and HTTP with a scripted provider; never contact a live model in pytest."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sisyphus.editor import Workshop
from sisyphus.sandbox import Podman
from sisyphus.state import Run

pytestmark = pytest.mark.podman


def call(name, args):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


@pytest.fixture
def model_lab(tmp_path, monkeypatch):
    image = os.environ.get("SISYPHUS_TEST_IMAGE")
    if not image:
        pytest.skip("Set SISYPHUS_TEST_IMAGE to an existing local image")
    replies, requests = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (
                    dict(self.headers),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            body = json.dumps(replies.pop(0)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    (tmp_path / "recipe").mkdir()
    (tmp_path / "recipe" / "hello").write_text("echo wrong\n")
    (tmp_path / "test_goal.py").write_text(
        "def test_goal(recipe):\n"
        '    assert recipe.run(["sh", "hello"]).stdout.strip() == "correct"\n'
        "\n# On-demand source marker, outside the failing assertion.\n"
    )
    (tmp_path / "sisyphus.toml").write_text(
        f"[recipe]\nimage={json.dumps(image)}\ntimeout=15\n"
        f'[editor]\nmodel="fake"\nbase_url="http://127.0.0.1:{server.server_port}/v1"\n'
        "timeout=30\n"
    )
    monkeypatch.setenv("TEST_PROVIDER_KEY", "host-credential-must-not-enter-container")
    try:
        yield tmp_path, replies, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for directory in (tmp_path / ".sisyphus" / "runs").glob("*"):
            run = Run.load(directory)
            Workshop(run, Podman()).remove()
            Podman().cleanup_trials(run.data["id"])


def solve(project):
    process = subprocess.run(
        [sys.executable, "-m", "sisyphus.cli", "solve", "test_goal.py", "--attempts", "2"],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return Run.load(next((project / ".sisyphus" / "runs").iterdir()))


def test_http_model_repairs_in_real_container_with_declared_access_only(model_lab):
    project, replies, requests = model_lab
    host_file = project / "host-only-secret"
    host_file.write_text("undeclared-host-data")
    replies.extend(
        [
            call("read_file", {"path": "/tests/test_goal.py", "offset": 0}),
            call(
                "shell",
                {
                    "command": 'set -eu; test -z "${TEST_PROVIDER_KEY:-}"; '
                    f"test ! -e {str(host_file)!r}; "
                    "test ! -e /run/podman/podman.sock; "
                    "if echo changed > /tests/test_goal.py; then exit 9; fi; "
                    "if echo changed > /evidence/0001/evidence/result.json; then exit 9; fi; "
                    "echo boundaries-ok",
                    "timeout": 5,
                },
            ),
            call("write_file", {"path": "/recipe/hello", "content": "echo correct\n"}),
            call("finish", {"status": "changed", "message": "fixed greeting"}),
        ]
    )
    run = solve(project)
    assert [entry["status"] for entry in run.data["attempts"]] == ["reject", "pass"]
    assert len(requests) == 4
    assert all("Authorization" not in headers for headers, _ in requests)
    assert "host-credential-must-not-enter-container" not in json.dumps(requests)
    assert "undeclared-host-data" not in json.dumps(requests)
    shell = json.loads((run.path / "repairs/0001/model-0002/tool-01/result.json").read_text())
    assert shell["returncode"] == 0
    assert shell["stdout"].strip() == "boundaries-ok"
    assert "On-demand source marker" in json.dumps(requests[1])
    assert "On-demand source marker" not in json.dumps(requests[0])
    assert host_file.read_text() == "undeclared-host-data"
    assert (run.path / "accepted/recipe/hello").read_text() == "echo correct\n"


def test_timed_out_model_command_cannot_keep_writing(model_lab):
    project, replies, requests = model_lab
    replies.extend(
        [
            call("shell", {"command": "sleep 2; touch /recipe/late", "timeout": 1}),
            call("shell", {"command": "sleep 2; test ! -e /recipe/late", "timeout": 5}),
            call("write_file", {"path": "/recipe/hello", "content": "echo correct\n"}),
            call("finish", {"status": "changed", "message": "fixed"}),
        ]
    )
    run = solve(project)
    root = run.path / "repairs/0001"
    first = json.loads((root / "model-0001/tool-01/result.json").read_text())
    second = json.loads((root / "model-0002/tool-01/result.json").read_text())
    assert first["timed_out"]
    assert second["returncode"] == 0
    assert not (project / "recipe/late").exists()
