from __future__ import annotations

import json
from pathlib import Path

from .errors import InfrastructureError
from .sandbox import Podman, Session, logged_process
from .state import Run


class Workshop(Session):
    """Persistent filesystem, frozen processes whenever host tests execute."""

    def __init__(self, run: Run, backend: Podman):
        backend.limits = run.config.limits
        backend.run_id = run.data["id"]
        super().__init__(
            backend,
            run.data["image"],
            run.config.candidate,
            {name: Path(path) for name, path in run.data["references"].items()},
        )
        self.run = run
        self.name = f"sisyphus-workshop-{run.data['id']}"
        self.role = "editor"
        (run.path / "research").mkdir(exist_ok=True)
        self.extra_mounts = [
            (run.path / "research", "/research", True),
            (run.path / "editor", "/editor", True),
            (run.path / "context", "/tests", True),
            (run.path / "attempts", "/evidence", True),
        ]

    def exists(self) -> bool:
        return (
            self.name
            in self.backend.control(
                "ps",
                "-a",
                "--filter",
                f"name=^{self.name}$",
                "--format",
                "{{.Names}}",
            ).splitlines()
        )

    def freeze(self) -> None:
        state = self.backend.control("inspect", "--format", "{{.State.Status}}", self.name)
        if state in {"created", "exited", "stopped"}:
            self.backend.control("start", self.name)
        if state != "paused":
            self.backend.control("pause", self.name)

    def ensure(self) -> None:
        if not self.exists():
            if self.run.data["workshop_created"]:
                raise InfrastructureError("Saved workshop container is missing; start a new run")
            self.backend.control(*self.create_args())
        self.run.data["workshop_created"] = True
        self.run.save()
        self.freeze()

    def repair(self, feedback_file: Path, directory: Path) -> dict:
        editor = self.run.config.editor
        assert editor is not None
        self.ensure()
        # Retain the filesystem, but do not revive an unfinished previous editor
        # or its background writers alongside the new repair command.
        self.backend.control("unpause", self.name)
        self.backend.control("stop", "--time=0", self.name)
        self.backend.control("start", self.name)
        try:
            if editor.model:
                from .model import repair

                return repair(self, feedback_file, directory)
            result = logged_process(
                ["podman", "exec", "--interactive", self.name, *editor.command],
                directory,
                editor.timeout,
                input_file=feedback_file,
            )
        finally:
            # This also freezes descendants that an editor left running.
            self.freeze()
        if result.returncode or result.timed_out or result.output_limited:
            raise InfrastructureError(f"Editor execution failed; see {directory}")
        response_file = directory / "stdout.log"
        if response_file.stat().st_size > 8192:
            raise InfrastructureError("Editor response exceeds 8 KiB; put diagnostics on stderr")
        try:
            response = json.loads(response_file.read_text())
            if (
                not isinstance(response, dict)
                or set(response) - {"version", "status", "message"}
                or response.get("version") != 1
                or response.get("status") not in {"changed", "blocked", "error"}
                or not isinstance(response.get("message", ""), str)
                or len(response.get("message", "")) > 2000
            ):
                raise ValueError("expected version 1 and status changed, blocked, or error")
        except (ValueError, OSError) as exc:
            raise InfrastructureError(f"Invalid editor response: {exc}") from exc
        return response

    def remove(self) -> None:
        self.backend.control("rm", "--force", "--ignore", "--time=0", self.name)
        self.run.data["workshop_created"] = False
        self.run.save()
