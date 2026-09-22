from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import InfrastructureError


def argv(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in value)
    ):
        raise InfrastructureError(f"{label} must be a nonempty argument list, e.g. ['sh', 'boot']")
    return tuple(value)


def duration(value: object, label: str = "timeout") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 86400:
        raise InfrastructureError(f"{label} must be a number of seconds between 0 and 86400")
    return float(value)


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def plain_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InfrastructureError(f"{label} must be a nonempty path")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    # Reject links in all path components before normalizing '..'.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InfrastructureError(f"{label} uses a symbolic link: {part}")
    return path.resolve()


@dataclass(frozen=True)
class Model:
    model: str
    base_url: str
    provider: str = "openai-compatible"
    api_key_env: str | None = None
    max_steps: int = 24

    @classmethod
    def load(cls, spec: dict) -> Model:
        if set(spec) - {
            "provider",
            "model",
            "base_url",
            "api_key_env",
            "max_steps",
            "timeout",
        }:
            raise InfrastructureError(
                "Model editor accepts provider, model, base_url, api_key_env, "
                "max_steps, and timeout"
            )
        provider = spec.get("provider", "openai-compatible")
        if provider not in {"openai-compatible", "openrouter"}:
            raise InfrastructureError(
                "editor.provider must be openai-compatible or openrouter"
            )
        model = spec.get("model")
        base = spec.get("base_url")
        if base is None and provider == "openrouter":
            base = "https://openrouter.ai/api/v1"
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise InfrastructureError("editor.model must be a nonempty model name")
        if not isinstance(base, str) or any(ord(c) <= 32 for c in base):
            raise InfrastructureError("editor.base_url must be an HTTP(S) API base URL")
        try:
            url = urlsplit(base)
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
            )
            _port = url.port  # Validate the port even before making a request.
        except ValueError:
            valid = False
        if not valid:
            raise InfrastructureError(
                "editor.base_url must be an HTTP(S) URL without credentials, query, or fragment"
            )
        key = spec.get(
            "api_key_env", "OPENROUTER_API_KEY" if provider == "openrouter" else None
        )
        if key is not None and (
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        ):
            raise InfrastructureError("editor.api_key_env must name a host environment variable")
        if key and url.scheme != "https":
            raise InfrastructureError("API credentials require an HTTPS endpoint")
        steps = spec.get("max_steps", 24)
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100:
            raise InfrastructureError("editor.max_steps must be an integer between 1 and 100")
        return cls(model, base.rstrip("/"), provider, key, steps)


def positive_int(value, label, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise InfrastructureError(f"{label} must be an integer from 1 to {maximum}")
    return value


@dataclass(frozen=True)
class Limits:
    memory_mb: int = 2048
    processes: int = 256
    snapshot_mb: int = 4096
    snapshot_files: int = 200000

    @classmethod
    def load(cls, spec):
        if not isinstance(spec, dict) or set(spec) - set(cls.__dataclass_fields__):
            raise InfrastructureError(
                "limits accepts memory_mb, processes, snapshot_mb, snapshot_files"
            )
        defaults = cls()
        return cls(
            **{
                name: positive_int(spec.get(name, getattr(defaults, name)), name, bound)
                for name, bound in [
                    ("memory_mb", 262144),
                    ("processes", 16384),
                    ("snapshot_mb", 1048576),
                    ("snapshot_files", 10000000),
                ]
            }
        )


@dataclass(frozen=True)
class Research:
    jina_key_env: str = "JINA_API_KEY"
    download_hosts: tuple[str, ...] = ()
    fetch_image: str = "docker.io/library/node:24-bookworm"
    max_mb: int = 512
    timeout: float = 300

    @classmethod
    def load(cls, spec):
        if not isinstance(spec, dict) or set(spec) - set(cls.__dataclass_fields__):
            raise InfrastructureError("Unknown research settings")
        defaults = cls()
        key = spec.get("jina_key_env", defaults.jina_key_env)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise InfrastructureError("research.jina_key_env must name a host environment variable")
        hosts = spec.get("download_hosts", [])
        if not isinstance(hosts, (list, tuple)) or any(
            not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
            for host in hosts
        ):
            raise InfrastructureError("research.download_hosts requires exact lowercase DNS names")
        image = spec.get("fetch_image", defaults.fetch_image)
        if (
            not isinstance(image, str)
            or not image
            or image.startswith("-")
            or any(c.isspace() or c == "\x00" for c in image)
        ):
            raise InfrastructureError("research.fetch_image must name a local image")
        return cls(
            key,
            tuple(hosts),
            image,
            positive_int(spec.get("max_mb", 512), "research.max_mb", 16384),
            duration(spec.get("timeout", 300), "research.timeout"),
        )


@dataclass(frozen=True)
class Editor:
    path: Path | None
    command: tuple[str, ...] = ()
    timeout: float = 300
    model: Model | None = None


@dataclass(frozen=True)
class Config:
    file: Path
    candidate: Path
    image: str
    setup: tuple[str, ...] | None
    timeout: float
    references: dict[str, Path]
    state: Path
    editor: Editor | None = None
    research: Research | None = None
    limits: Limits = Limits()

    @classmethod
    def load(cls, file: Path) -> Config:
        file = plain_path(Path.cwd(), str(file), "configuration")
        try:
            with file.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, ValueError) as exc:
            raise InfrastructureError(f"Cannot read {file}: {exc}") from exc
        unknown = set(data) - {"recipe", "references", "editor", "research", "limits"}
        if unknown:
            raise InfrastructureError(
                f"Unknown configuration keys: {', '.join(sorted(map(str, unknown)))}"
            )
        spec = data.get("recipe", {})
        if not isinstance(spec, dict):
            raise InfrastructureError("recipe must be a table")
        unknown = set(spec) - {"path", "image", "setup", "timeout"}
        if unknown:
            raise InfrastructureError(
                f"Unknown recipe keys: {', '.join(sorted(map(str, unknown)))}"
            )
        root = file.parent
        candidate = plain_path(root, spec.get("path", "recipe"), "recipe.path")
        if candidate.exists() and not candidate.is_dir():
            raise InfrastructureError(f"Recipe path is not a directory: {candidate}")
        if candidate == root or candidate in root.parents:
            raise InfrastructureError("The editable recipe must not contain the trusted project")
        image = spec.get("image", "docker.io/library/ubuntu:24.04")
        if (
            not isinstance(image, str)
            or not image
            or image.startswith("-")
            or any(c.isspace() or c == "\x00" for c in image)
        ):
            raise InfrastructureError("recipe.image must be a container image reference")
        setup = argv(spec["setup"], "recipe.setup") if "setup" in spec else None
        refs = data.get("references", {})
        if not isinstance(refs, dict):
            raise InfrastructureError("references must be a table of names and directory paths")
        references = {}
        state = plain_path(root, ".sisyphus", "state")
        paths = [("candidate", candidate), ("state", state)]
        editor = None
        if "editor" in data:
            spec_editor = data["editor"]
            if not isinstance(spec_editor, dict):
                raise InfrastructureError("editor must be a table")
            if "model" in spec_editor:
                editor = Editor(
                    None,
                    timeout=duration(spec_editor.get("timeout", 300)),
                    model=Model.load(spec_editor),
                )
            elif set(spec_editor) - {
                "path",
                "command",
                "timeout",
            }:
                raise InfrastructureError("editor accepts only path, command, and timeout")
            else:
                editor_path = plain_path(root, spec_editor.get("path", "editor"), "editor.path")
                if not editor_path.is_dir():
                    raise InfrastructureError(f"Editor directory does not exist: {editor_path}")
                editor = Editor(
                    editor_path,
                    argv(spec_editor.get("command"), "editor.command"),
                    duration(spec_editor.get("timeout", 300), "editor.timeout"),
                )
                paths.append(("editor", editor_path))
        for name, value in refs.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
                raise InfrastructureError(f"Invalid reference name: {name!r}")
            path = plain_path(root, value, f"reference {name}")
            if not path.is_dir():
                raise InfrastructureError(f"Reference directory does not exist: {path}")
            references[name] = path
            paths.append((f"reference {name}", path))
        for index, (label, path) in enumerate(paths):
            for other_label, other_path in paths[index + 1 :]:
                if overlaps(path, other_path):
                    raise InfrastructureError(f"{label} and {other_label} overlap")
        config = cls(
            file,
            candidate,
            image,
            setup,
            duration(spec.get("timeout", 300)),
            references,
            state,
            editor,
            Research.load(data["research"]) if "research" in data else None,
            Limits.load(data.get("limits", {})),
        )

        # Validate every setting and overlap before creating an empty deliverable.
        candidate.mkdir(parents=True, exist_ok=True)
        return config

    def record(self) -> dict:
        return {
            "file": str(self.file),
            "candidate": str(self.candidate),
            "image": self.image,
            "setup": list(self.setup) if self.setup else None,
            "timeout": self.timeout,
            "references": {name: str(path) for name, path in self.references.items()},
            "state": str(self.state),
            "research": asdict(self.research) if self.research else None,
            "limits": asdict(self.limits),
            "editor": {
                "path": str(self.editor.path) if self.editor.path else None,
                "command": list(self.editor.command),
                "timeout": self.editor.timeout,
                "model": asdict(self.editor.model) if self.editor.model else None,
            }
            if self.editor
            else None,
        }

    @classmethod
    def restore(cls, data: dict) -> Config:
        """Restore host-owned run metadata without re-reading changed live references."""
        editor = data["editor"]
        return cls(
            Path(data["file"]),
            Path(data["candidate"]),
            data["image"],
            argv(data["setup"], "saved setup") if data["setup"] else None,
            duration(data["timeout"]),
            {name: Path(path) for name, path in data["references"].items()},
            Path(data["state"]),
            Editor(
                Path(editor["path"]) if editor["path"] else None,
                argv(editor["command"], "saved editor") if editor["command"] else (),
                duration(editor["timeout"]),
                Model.load(editor["model"]) if editor.get("model") else None,
            )
            if editor
            else None,
            Research.load(data["research"]) if data.get("research") else None,
            Limits.load(data.get("limits", {})),
        )


def discover(start: Path) -> Path | None:
    start = start.resolve()
    if start.is_file():
        start = start.parent
    # An enclosing project owns its candidate/reference/state boundaries. A file written
    # inside those directories cannot turn itself into a trusted nested project.
    for directory in reversed((start, *start.parents)):
        path = directory / "sisyphus.toml"
        if path.is_file():
            return path
    return None
