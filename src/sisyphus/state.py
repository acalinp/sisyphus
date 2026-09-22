from __future__ import annotations

import fcntl
import importlib.metadata
import json
import os
import sys
import uuid
from pathlib import Path

from .config import Config, duration, plain_path
from .errors import InfrastructureError
from .evidence import write_json
from .snapshot import snapshot


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (OSError, ValueError) as exc:
        raise InfrastructureError(f"Cannot read {path}: {exc}") from exc


class ProjectLock:
    """One solve process per project; the pytest child inherits the lease."""

    def __init__(self, state: Path):
        self.state = state
        self.fd: int | None = None

    def __enter__(self) -> ProjectLock:
        self.state.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.state / "solve.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise InfrastructureError(
                "Another solver or its test process holds the project lock"
            ) from exc
        return self

    def __exit__(self, *args) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def environment_identity() -> dict:
    return {
        "python": sys.version,
        "packages": sorted(
            [dist.metadata["Name"], dist.version] for dist in importlib.metadata.distributions()
        ),
    }


def trusted_digest(config: Config) -> str:
    root = config.file.parent
    excluded = [config.candidate, config.state, *config.references.values()]
    ignored = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__"}
    for directory, names, _files in os.walk(root):
        for name in list(names):
            path = Path(directory) / name
            if name in ignored or path in excluded:
                names.remove(name)
                excluded.append(path)
    return snapshot(root, exclude=tuple(excluded), limits=config.limits)


def select_test(config: Config, value: str) -> str:
    file, *node = value.split("::")
    path = plain_path(Path.cwd(), file, "test")
    if not path.is_file() or path.suffix != ".py" or not path.is_relative_to(config.file.parent):
        raise InfrastructureError("Select one trusted Python test file inside the project")
    for root in (config.candidate, config.state, *config.references.values()):
        if path == root or root in path.parents:
            raise InfrastructureError(
                "Cannot select a recipe, reference, or evidence file as a test"
            )
    return "::".join([str(path), *node])


def attempt_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise InfrastructureError("Attempt limit must be an integer between 1 and 1000")
    return value


class Run:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self.config = Config.restore(data["settings"])

    @classmethod
    def create(cls, config: Config, selection: str, limit: int, timeout: float, backend) -> Run:
        if config.editor is None:
            raise InfrastructureError(
                "solve requires editor settings: model or path and command"
            )
        selection = select_test(config, selection)
        limit = attempt_limit(limit)
        timeout = duration(timeout, "test timeout")
        image = backend.resolve(config.image)
        run_id = uuid.uuid4().hex
        path = config.state / "runs" / run_id
        path.mkdir(parents=True, mode=0o700)
        data = {
            "version": 1,
            "id": run_id,
            "settings": config.record(),
            "selection": selection,
            "image": image,
            "limit": limit,
            "test_timeout": timeout,
            "attempts": [],
            "repairs": [],
            "phase": "ready",
            "collected": None,
            "environment": environment_identity(),
            "trusted_digest": trusted_digest(config),
            "references": {},
            "reference_digests": {},
            "editor_digest": None,
            "workshop_created": False,
        }
        run = cls(path, data)
        run.save()
        for name, source in config.references.items():
            target = path / "references" / name
            data["reference_digests"][name] = snapshot(source, target, limits=config.limits)
            data["references"][name] = str(target)
        editor_source = config.editor.path or Path(__file__).parent / "agent"
        data["editor_digest"] = snapshot(editor_source, path / "editor")
        context = path / "context"
        context.mkdir()
        test = Path(selection.split("::", 1)[0])
        (context / test.name).write_bytes(test.read_bytes())
        (path / "attempts").mkdir()
        (path / "repairs").mkdir()
        run.save()
        return run

    @classmethod
    def load(cls, path: Path) -> Run:
        path = plain_path(Path.cwd(), str(path), "run")
        data = read_json(path / "run.json")
        try:
            if data["version"] != 1 or data["id"] != path.name:
                raise ValueError("unsupported run version or identity")
            run = cls(path, data)
            if path.parent != run.config.state / "runs":
                raise ValueError("run is outside its recorded state directory")
            attempt_limit(data["limit"])
            duration(data["test_timeout"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InfrastructureError(f"Invalid saved run: {exc}") from exc
        return run

    def save(self) -> None:
        write_json(self.path / "run.json", self.data)

    def verify_trusted(self) -> None:
        if environment_identity() != self.data["environment"]:
            raise InfrastructureError("Python or installed packages changed; start a new run")
        if trusted_digest(self.config) != self.data["trusted_digest"]:
            raise InfrastructureError("Trusted project files changed; start a new run")
        if snapshot(self.path / "editor") != self.data["editor_digest"]:
            raise InfrastructureError("Saved editor input changed")
        for name, path in self.data["references"].items():
            if (
                snapshot(Path(path), limits=self.config.limits)
                != self.data["reference_digests"][name]
            ):
                raise InfrastructureError(f"Saved reference changed: {name}")

        for identifier, metadata in self.data.get("research_items", {}).items():
            directory = self.path / "research" / identifier
            if (
                snapshot(directory, exclude=(directory / "origin.json",), limits=self.config.limits)
                != metadata["tree_sha256"]
                or read_json(directory / "origin.json") != metadata
            ):
                raise InfrastructureError("Saved research input changed")

    def set_phase(self, phase: str, **values) -> None:
        self.data.update(phase=phase, **values)
        self.save()
