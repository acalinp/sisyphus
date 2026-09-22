import sys

import pytest

from sisyphus import InfrastructureError
from sisyphus.sandbox import Podman, Session, logged_process


def test_container_arguments_only_expose_declared_paths(tmp_path):
    session = Session(Podman(), "sha256:" + "a" * 64, tmp_path / "work", {"docs": tmp_path / "ref"})
    args = session.create_args()
    assert "--network=none" in args
    assert "--pull=never" in args
    assert "--http-proxy=false" in args
    assert "--image-volume=ignore" in args
    assert "--userns=keep-id:uid=0,gid=0" in args
    mounts = [args[index + 1] for index, value in enumerate(args) if value == "--mount"]
    assert mounts == [
        f"type=bind,src={tmp_path / 'work'},dst=/recipe,rw",
        f"type=bind,src={tmp_path / 'ref'},dst=/refs/docs,ro",
    ]
    assert not any("--privileged" in arg or "podman.sock" in arg for arg in args)


def test_mount_option_injection_is_rejected(tmp_path):
    session = Session(Podman(), "image", tmp_path / "bad,ro=false", {})
    with pytest.raises(InfrastructureError, match="commas"):
        session.create_args()


def test_streams_are_drained_and_retained_separately(tmp_path):
    code = "import sys; sys.stdout.write('a'*200000); sys.stderr.write('b'*200000)"
    result = logged_process([sys.executable, "-c", code], tmp_path, 5)
    assert result.returncode == 0
    assert (tmp_path / "stdout.log").read_bytes() == b"a" * 200000
    assert (tmp_path / "stderr.log").read_bytes() == b"b" * 200000


def test_process_timeout(tmp_path):
    result = logged_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.1)
    assert result.timed_out
    assert result.returncode != 0


def test_log_storage_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr("sisyphus.sandbox.LOG_LIMIT", 1000)
    result = logged_process([sys.executable, "-c", "print('a'*200000)"], tmp_path, 5)
    assert result.output_limited
    assert (tmp_path / "stdout.log").stat().st_size == 1000


def test_partial_startup_attempts_container_removal(tmp_path):
    class Broken(Podman):
        def __init__(self):
            self.calls = []

        def control(self, *args):
            self.calls.append(args)
            if args[0] == "start":
                raise InfrastructureError("start failed")
            return ""

    backend = Broken()
    with pytest.raises(InfrastructureError, match="start failed"):
        with backend.session("image", tmp_path, {}):
            pass
    assert backend.calls[-1][:3] == ("rm", "--force", "--ignore")


def test_cleanup_failure_cannot_be_accepted(tmp_path):
    class Broken(Podman):
        def control(self, *args):
            if args[0] == "rm":
                raise InfrastructureError("permission denied")
            return ""

    with pytest.raises(InfrastructureError, match="cleanup failed"):
        with Broken().session("image", tmp_path, {}):
            pass


def test_rootful_engine_is_rejected(monkeypatch):
    monkeypatch.setattr(Podman, "control", lambda *args: "false")
    with pytest.raises(InfrastructureError, match="rootless"):
        Podman().resolve("image")
