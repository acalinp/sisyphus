from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .config import Limits
from .errors import InfrastructureError


def snapshot(
    source: Path,
    target: Path | None = None,
    *,
    exclude: tuple[Path, ...] = (),
    limits: Limits | None = None,
) -> str:
    """Freeze a bounded tree using directory FDs; preserve only contained relative links."""
    limits = limits or Limits()
    digest = hashlib.sha256()
    count = size = 0
    links = []
    if target is not None:
        target.mkdir(parents=True, exist_ok=False)

    def visit(fd, relative, destination):
        nonlocal count, size
        for name in sorted(os.listdir(fd)):
            path = source / relative / name
            if any(path == omitted or omitted in path.parents for omitted in exclude):
                continue
            count += 1
            if count > limits.snapshot_files:
                raise InfrastructureError("Snapshot exceeds its file-count limit")
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            rel = relative / name
            encoded = rel.as_posix().encode()
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
            output = destination / name if destination is not None else None
            if stat.S_ISDIR(mode):
                digest.update(b"d")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if output is not None:
                        output.mkdir()
                    visit(child, rel, output)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(mode):
                value = os.readlink(name, dir_fd=fd)
                if Path(value).is_absolute():
                    raise InfrastructureError(f"Absolute symbolic link is unsupported: {path}")
                try:
                    resolved = path.resolve(strict=False)
                except (ValueError, RuntimeError) as exc:
                    raise InfrastructureError(f"Invalid symbolic link: {path}") from exc
                if not resolved.is_relative_to(source.resolve()):
                    raise InfrastructureError(f"Escaping symbolic link is unsupported: {path}")
                raw = os.fsencode(value)
                digest.update(b"l" + len(raw).to_bytes(8, "big") + raw)
                if output is not None:
                    output.symlink_to(value)
                    links.append(output)
            elif stat.S_ISREG(mode):
                executable = bool(mode & 0o111)
                digest.update(b"x" if executable else b"f")
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise InfrastructureError(f"Not a regular file: {path}")
                    content_digest = hashlib.sha256()
                    result = output.open("xb") if output is not None else None
                    try:
                        while block := handle.read(1024 * 1024):
                            size += len(block)
                            if size > limits.snapshot_mb * 1024**2:
                                raise InfrastructureError("Snapshot exceeds its byte limit")
                            if result is not None:
                                result.write(block)
                            content_digest.update(block)
                    finally:
                        if result is not None:
                            result.close()
                    digest.update(content_digest.digest())
                if output is not None:
                    output.chmod(0o755 if executable else 0o644)
            else:
                raise InfrastructureError(
                    f"Snapshots require regular files; unsupported path: {path}"
                )

    try:
        root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            visit(root, Path(), target)
        finally:
            os.close(root)
        for link in links:
            if not link.resolve(strict=False).is_relative_to(target.resolve()):
                raise InfrastructureError("Snapshot link escaped the saved tree")
    except (OSError, RuntimeError) as exc:
        raise InfrastructureError(f"Cannot snapshot {source}: {exc}") from exc
    return digest.hexdigest()
