from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Limits
from .errors import InfrastructureError

LOG_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class Completed:
    returncode: int
    timed_out: bool = False
    output_limited: bool = False


def logged_process(
    args: list[str],
    directory: Path,
    timeout: float,
    *,
    input_file: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    keep_stdin: bool = False,
    pass_fds: tuple[int, ...] = (),
) -> Completed:
    """Drain both pipes concurrently, retaining at most LOG_LIMIT bytes each."""
    limited = threading.Event()
    errors: list[Exception] = []
    incoming = input_file.open("rb") if input_file else None
    try:
        process = subprocess.Popen(
            args,
            stdin=incoming if incoming else (subprocess.PIPE if keep_stdin else subprocess.DEVNULL),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            env=env,
            pass_fds=pass_fds,
        )
    except OSError as exc:
        raise InfrastructureError(f"Cannot start process: {exc}") from exc
    finally:
        if incoming is not None:
            incoming.close()

    def drain(stream, file: Path):
        try:
            with stream, file.open("xb") as output:
                remaining = LOG_LIMIT
                while block := stream.read(65536):
                    output.write(block[:remaining])
                    remaining = max(0, remaining - len(block))
                    if remaining == 0:
                        limited.set()
        except Exception as exc:
            errors.append(exc)
            limited.set()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, directory / "stdout.log")),
        threading.Thread(target=drain, args=(process.stderr, directory / "stderr.log")),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while process.poll() is None:
            timed_out = time.monotonic() >= deadline
            if timed_out or limited.is_set():
                break
            time.sleep(0.02)
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        if process.stdin:
            process.stdin.close()
        for thread in threads:
            thread.join()
    if errors:
        raise InfrastructureError(f"Cannot retain command output: {errors[0]}") from errors[0]
    return Completed(process.returncode, timed_out, limited.is_set())


class Podman:
    run_id: str | None = None
    limits: Limits = Limits()

    def control(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["podman", *args],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfrastructureError(f"Podman control command failed: {exc}") from exc
        if result.returncode:
            raise InfrastructureError(
                f"Podman {' '.join(args[:2])}: {result.stderr.strip()[:2000]}"
            )
        return result.stdout.strip()

    def resolve(self, image: str) -> str:
        if self.control("info", "--format", "{{.Host.Security.Rootless}}") != "true":
            raise InfrastructureError("Sisyphus requires rootless Podman")
        identity = self.control("image", "inspect", "--format", "{{.Id}}", image)
        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", identity):
            raise InfrastructureError(f"Podman returned an invalid image identity: {identity!r}")
        return identity

    def session(self, image: str, work: Path, references: dict[str, Path]) -> Session:
        return Session(self, image, work, references)

    def cleanup_trials(self, run_id: str) -> None:
        containers = self.control(
            "ps",
            "-a",
            "--filter",
            f"label=io.sisyphus.run={run_id}",
            "--filter",
            "label=io.sisyphus.role=replay",
            "--format",
            "{{.ID}}",
        ).splitlines()
        if containers:
            self.control("rm", "--force", "--ignore", "--time=0", *containers)


class Session:
    def __init__(self, backend: Podman, image: str, work: Path, references: dict[str, Path]):
        self.backend = backend
        self.image = image
        self.work = work
        self.references = references
        self.name = f"sisyphus-{uuid.uuid4().hex}"
        self.role = "replay"
        self.extra_mounts: list[tuple[Path, str, bool]] = []

    def create_args(self) -> list[str]:
        args = [
            "create",
            "--name",
            self.name,
            "--pull=never",
            "--network=none",
            "--http-proxy=false",
            "--image-volume=ignore",
            "--userns=keep-id:uid=0,gid=0",
            "--user=0:0",
            "--security-opt=no-new-privileges",
            f"--pids-limit={self.backend.limits.processes}",
            f"--memory={self.backend.limits.memory_mb}m",
            "--workdir=/recipe",
            "--entrypoint=/bin/sh",
        ]
        if self.backend.run_id:
            args.extend(
                [
                    "--label",
                    f"io.sisyphus.run={self.backend.run_id}",
                    "--label",
                    f"io.sisyphus.role={self.role}",
                ]
            )
        for source, target, readonly in [
            (self.work, "/recipe", False),
            *((path, f"/refs/{name}", True) for name, path in self.references.items()),
            *self.extra_mounts,
        ]:
            if any(character in str(source) for character in (",", "\n", "\r")):
                raise InfrastructureError(
                    "Container mount source paths cannot contain commas/newlines"
                )
            mount = f"type=bind,src={source},dst={target},{'ro' if readonly else 'rw'}"
            args.extend(["--mount", mount])
        return [*args, self.image, "-c", "exec sleep infinity"]

    def __enter__(self) -> Session:
        # Attempt removal even if create/start only partially succeeds.
        try:
            self.backend.control(*self.create_args())
            self.backend.control("start", self.name)
        except BaseException:
            self.backend.control("rm", "--force", "--ignore", "--time=0", self.name)
            raise
        return self

    def execute(
        self,
        args: tuple[str, ...],
        env: dict[str, str],
        directory: Path,
        timeout: float,
    ) -> Completed:
        command = ["podman", "exec"]
        for key, value in sorted(env.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend([self.name, *args])
        result = logged_process(command, directory, timeout)
        if result.returncode == 125 and not result.timed_out and not result.output_limited:
            raise InfrastructureError(
                f"Podman exec failed (reserved exit 125); see {directory / 'stderr.log'}"
            )
        return result

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.backend.control("rm", "--force", "--ignore", "--time=0", self.name)
        except InfrastructureError as cleanup:
            raise InfrastructureError(f"Container cleanup failed: {cleanup}") from exc
