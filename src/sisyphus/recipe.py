from __future__ import annotations

import json
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .config import argv, duration
from .errors import InfrastructureError, RecipeRejected
from .evidence import Attempt
from .sandbox import Completed, Podman
from .snapshot import snapshot


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout_path: Path
    stderr_path: Path
    directory: Path

    @property
    def stdout(self) -> str:
        return self.stdout_path.read_text(errors="replace")

    @property
    def stderr(self) -> str:
        return self.stderr_path.read_text(errors="replace")

    def file(self, relative: str) -> Path:
        """Locate a candidate-relative regular output without following links."""
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise InfrastructureError("Output paths must be candidate-relative without '..'")
        target = self.directory / path
        for component in (target, *target.parents):
            if component == self.directory:
                break
            if component.is_symlink():
                raise RecipeRejected(f"Output path uses a symbolic link: {relative}")
        try:
            mode = target.stat().st_mode
        except FileNotFoundError as exc:
            raise RecipeRejected(f"Required output is missing: {relative}") from exc
        if not stat.S_ISREG(mode):
            raise RecipeRejected(f"Output is not a regular file: {relative}")
        return target


class Recipe:
    def __init__(self, attempt: Attempt, nodeid: str, backend: Podman | None = None):
        self.attempt = attempt
        self.nodeid = nodeid
        self.backend = backend if backend is not None else Podman()
        self.backend.limits = attempt.config.limits
        self.backend.run_id = attempt.run_id

    def run(
        self,
        command: list[str] | tuple[str, ...],
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
        access: tuple = (),
    ) -> Result:
        args = argv(command, "recipe.run command")
        seconds = duration(self.attempt.config.timeout if timeout is None else timeout)
        if not isinstance(check, bool):
            raise InfrastructureError("check must be a boolean")
        environment = {} if env is None else dict(env)
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key.startswith("SISYPHUS_")
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise InfrastructureError("Invalid environment; SISYPHUS_* names are reserved")
        from .capability import Capability

        if not isinstance(access, (list, tuple)) or any(
            not isinstance(c, Capability) for c in access
        ):
            raise InfrastructureError("access must contain trusted active Capability objects")
        if len({c.name for c in access}) != len(access):
            raise InfrastructureError("Capability names must be unique")
        source = self.attempt.freeze()
        if self.attempt.image is None:
            self.attempt.image = self.backend.resolve(self.attempt.config.image)
            self.attempt.flush()
        work = self.attempt.path / "work" / str(len(self.attempt.commands) + 1)
        snapshot(source, work, limits=self.attempt.config.limits)
        deadline = time.monotonic() + seconds

        session = self.backend.session(self.attempt.image, work, self.attempt.references)
        if access:
            endpoints = dict(c.grant(session, environment) for c in access)
            session.extra_mounts.append((Path(__file__).parent / "runtime", "/sisyphus", True))
            environment["SISYPHUS_CAPABILITIES"] = json.dumps(endpoints)
        with session:
            if self.attempt.config.setup:
                self._execute(
                    session, self.attempt.config.setup, environment, "setup", deadline, True
                )
            completed, directory = self._execute(
                session,
                args,
                environment,
                "recipe",
                deadline,
                check,
            )
        for capability in access:
            capability.check()
        return Result(
            completed.returncode, directory / "stdout.log", directory / "stderr.log", work
        )

    def _execute(self, session, args, env, kind, deadline, check) -> tuple[Completed, Path]:
        directory, record = self.attempt.command(self.nodeid, args, kind)
        started = time.monotonic()
        try:
            remaining = deadline - started
            if remaining <= 0:
                raise RecipeRejected("Recipe time budget was exhausted before command execution")
            result = session.execute(args, env, directory, remaining)
            record.update(
                returncode=result.returncode,
                timed_out=result.timed_out,
                output_limited=result.output_limited,
                status="failed"
                if result.returncode or result.timed_out or result.output_limited
                else "passed",
            )
            if result.timed_out:
                raise RecipeRejected(f"{kind} command timed out; evidence: {directory}")
            if result.output_limited:
                raise RecipeRejected(
                    f"{kind} command exceeded the output limit; evidence: {directory}"
                )
            if check and result.returncode:
                raise RecipeRejected(
                    f"{kind} command exited {result.returncode}; evidence: {directory}"
                )
            return result, directory
        except InfrastructureError:
            record["status"] = "error"
            raise
        except RecipeRejected:
            record["status"] = "failed"
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.attempt.flush()
