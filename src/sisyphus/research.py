"""Research yields immutable local files. Build inputs are vendored into the recipe."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .evidence import write_json
from .network import FetchError, Tunnel, fetch, public_url
from .sandbox import Podman, Session
from .snapshot import snapshot


class ResearchStore:
    def __init__(self, run):
        self.run = run
        self.settings = run.config.research
        self.root = run.path / "research"
        self.root.mkdir(exist_ok=True)

    def call(self, name, args, *, timeout=None):
        if self.settings is None:
            raise FetchError("Research is not enabled in trusted configuration")
        self.timeout = min(self.settings.timeout, timeout or self.settings.timeout)
        directory = self.root / (".incoming-" + uuid.uuid4().hex)
        directory.mkdir()
        try:
            if name == "clone_repo":
                metadata, item = self.clone(args, directory)
            elif name == "download":
                url = args["url"]
                public_url(url, self.settings.download_hosts)
                expected = args["sha256"]
                if not isinstance(expected, str) or (
                    expected and not re.fullmatch(r"[0-9a-f]{64}", expected)
                ):
                    raise FetchError("sha256 must be empty or a lowercase SHA-256 digest")
                item = "content"
                with (directory / item).open("wb") as out:
                    received = fetch(
                        url,
                        out,
                        hosts=self.settings.download_hosts,
                        redirects=3,
                        limit=self.settings.max_mb * 1024**2,
                        timeout=self.timeout,
                    )
                digest = file_hash(directory / item)
                if expected and digest != expected:
                    raise FetchError("Downloaded bytes do not match the requested SHA-256")
                metadata = {"kind": name, "url": url, "sha256": digest, **received}
            else:
                metadata, item = self.jina(name, args, directory)
            digest = snapshot(directory, limits=self.run.config.limits)
            metadata.update(tree_sha256=digest, retrieved_at=time.time())
            write_json(directory / "origin.json", metadata)
            identifier = uuid.uuid4().hex[:16]
            target = self.root / identifier
            directory.rename(target)
            self.run.data.setdefault("research_items", {})[identifier] = metadata
            self.run.save()
            result = {
                "path": f"/research/{identifier}/{item}",
                "origin": f"/research/{identifier}/origin.json",
                **metadata,
            }
            if item == "document.md":
                result["preview"] = (target / item).read_text()[:2000]
            result["instruction"] = (
                "Read/search this saved input with sandbox tools. Copy required "
                "build inputs and origin.json into /recipe/vendor; replay is offline "
                "and does not mount /research."
            )
            return result
        except FetchError as exc:
            if (directory / "fetch-logs").exists():
                name = "failed-" + uuid.uuid4().hex[:16]
                directory.rename(self.root / name)
                raise FetchError(f"{exc}; diagnostics: /research/{name}/fetch-logs") from exc
            raise
        finally:
            if directory.exists():
                shutil.rmtree(directory)

    def jina(self, name, args, directory):
        key = os.environ.get(self.settings.jina_key_env, "")
        if any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise FetchError("Jina credential contains invalid header characters")
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        if name == "search":
            if not key:
                raise FetchError(f"Search requires host environment {self.settings.jina_key_env}")
            query = args["query"]
            if not isinstance(query, str) or not 1 <= len(query) <= 2000:
                raise FetchError("query must contain 1 to 2000 characters")
            url = "https://s.jina.ai/" + quote(query, safe="")
            headers["X-Respond-With"] = "no-content"
            origin = {"kind": name, "query": query}
        elif name == "read_url":
            public_url(args["url"])
            url = "https://r.jina.ai/" + quote(args["url"], safe=":/?=&%")
            origin = {"kind": name, "url": args["url"]}
        else:
            raise FetchError("Unknown research tool")
        output = io.BytesIO()
        fetch(url, output, headers=headers, limit=8 * 1024**2, timeout=min(60, self.timeout))
        raw = output.getvalue().decode("utf-8", errors="replace")
        if key:
            raw = raw.replace(key, "[REDACTED]")
        try:
            document = json.loads(raw)
            data = document["data"]
            if name == "search":
                if not isinstance(data, list):
                    raise ValueError("expected search list")
                raw = "\n\n".join(
                    f"{entry.get('title', '')}\n{entry.get('url', '')}\n"
                    f"{str(entry.get('description', ''))[:500]}"
                    for entry in data[:5]
                )
            else:
                raw = f"# {data.get('title', '')}\n\n{data['content']}"
        except (ValueError, TypeError, KeyError) as exc:
            raise FetchError("Jina returned an invalid document") from exc
        (directory / "document.md").write_text(raw, encoding="utf-8")
        origin["sha256"] = file_hash(directory / "document.md")
        return origin, "document.md"

    def clone(self, args, directory):
        url, ref = args["url"], args["ref"]
        parsed = public_url(url, self.settings.download_hosts)
        if parsed.query:
            raise FetchError("Repository URLs cannot contain query parameters")
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", ref):
            raise FetchError("ref must be an explicit branch, tag, or commit")
        backend = Podman()
        backend.run_id = self.run.data["id"]
        backend.limits = self.run.config.limits
        if not self.run.data.get("fetch_image"):
            self.run.data["fetch_image"] = backend.resolve(self.settings.fetch_image)
            self.run.save()
        # Unix paths stay short regardless of the user's project/run directory length.
        with tempfile.TemporaryDirectory(prefix="sisyphus-fetch-") as temporary:
            broker = Path(temporary)
            write_json(
                broker / "request.json",
                {
                    "url": url,
                    "ref": ref,
                    "max_bytes": self.run.config.limits.snapshot_mb * 1024**2,
                    "max_files": self.run.config.limits.snapshot_files,
                },
            )
            with Tunnel(
                broker / "socket",
                self.settings.download_hosts,
                limit=self.settings.max_mb * 1024**2,
                timeout=self.timeout,
            ):
                session = Session(backend, self.run.data["fetch_image"], directory, {})
                session.extra_mounts = [
                    (broker, "/broker", True),
                    (broker / "request.json", "/request.json", True),
                    (self.run.path / "editor", "/editor", True),
                ]
                logs = broker / "logs"
                logs.mkdir()
                with session:
                    result = session.execute(
                        ("python3", "/editor/fetch_git.py"), {}, logs, self.timeout
                    )
                shutil.copytree(logs, directory / "fetch-logs")
                if result.returncode or result.timed_out or result.output_limited:
                    raise FetchError(
                        "Git fetch failed; check URL/ref, byte budget, and fetch image "
                        "(requires git, python3, and CA certificates)"
                    )
        commit = (directory / "commit.txt").read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise FetchError("Git returned an invalid commit identity")
        return {"kind": "git", "url": url, "ref": ref, "commit": commit}, "repo"


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024**2):
            digest.update(block)
    return digest.hexdigest()
