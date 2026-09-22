from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .config import Config
from .errors import InfrastructureError
from .snapshot import snapshot


def write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class Attempt:
    def __init__(self, config: Config, trial: dict | None = None):
        self.config = config
        self.path = (
            Path(trial["evidence"])
            if trial
            else config.state / "attempts" / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        )
        self.path.mkdir(parents=True, mode=0o700)
        self.commands: list[dict] = []
        self.reports: list[dict] = []
        self.image: str | None = trial["image"] if trial else None
        self.source: Path | None = Path(trial["source"]) if trial else None
        self.references: dict[str, Path] = (
            {name: Path(path) for name, path in trial["references"].items()} if trial else {}
        )
        self.digests: dict[str, str] = dict(trial["digests"]) if trial else {}
        self.run_id: str | None = trial["run_id"] if trial else None
        self.collected: list[str] = []
        self.deselected: list[str] = []
        self.started = time.time()
        self.outcome = "incomplete"
        self.flush()

    def freeze(self) -> Path:
        if self.source is None:
            source = self.path / "source"
            self.digests["recipe"] = snapshot(
                self.config.candidate, source, limits=self.config.limits
            )
            for name, path in self.config.references.items():
                target = self.path / "references" / name
                self.digests[f"reference:{name}"] = snapshot(
                    path, target, limits=self.config.limits
                )
                self.references[name] = target
            self.source = source
            self.flush()
        return self.source

    def command(self, nodeid: str, args: tuple[str, ...], kind: str) -> tuple[Path, dict]:
        directory = self.path / "commands" / f"{len(self.commands) + 1:04d}"
        directory.mkdir(parents=True)
        record = {
            "nodeid": nodeid,
            "kind": kind,
            "argv": list(args),
            "status": "running",
            "stdout": str((directory / "stdout.log").relative_to(self.path)),
            "stderr": str((directory / "stderr.log").relative_to(self.path)),
        }
        self.commands.append(record)
        self.flush()
        return directory, record

    def attach(self, source: Path, *, name: str, description: str = "") -> Path:
        """Copy a trusted observation into evidence without following symlinks."""
        if not name or Path(name).name != name or name in {".", ".."}:
            raise InfrastructureError("Evidence attachment names must be single filenames")
        if source.is_symlink() or not source.is_file():
            raise InfrastructureError("Evidence attachments must be regular files")
        directory = self.path / "attachments"
        directory.mkdir(exist_ok=True)
        target = directory / name
        # Exclusive creation avoids silently replacing an earlier observation.
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            while block := incoming.read(1024 * 1024):
                outgoing.write(block)
        write_json(target.with_name(f"{name}.metadata.json"), {"description": description})
        return target

    def flush(self) -> None:
        write_json(
            self.path / "result.json",
            {
                "version": 1,
                "outcome": self.outcome,
                "started": self.started,
                "configuration": str(self.config.file),
                "image": self.image,
                "digests": self.digests,
                "commands": self.commands,
                "tests": self.reports,
                "run_id": self.run_id,
                "collected": self.collected,
                "deselected": self.deselected,
            },
        )
