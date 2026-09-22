"""Trusted, explicitly granted Unix-socket endpoints; never arbitrary host commands."""

from __future__ import annotations

import contextlib
import json
import re
import socketserver
import tempfile
import threading
from pathlib import Path

from .errors import InfrastructureError

REQUEST_LIMIT = 12 * 1024**2  # Includes base64 firmware payloads.


class Capability:
    def __init__(self, name, handler):
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise InfrastructureError("Invalid capability name")
        self.name, self.handler = name, handler
        self.active = False
        self.error = None

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sisyphus-cap-")
        self.directory = Path(self.temporary.name)
        try:
            self.server = socketserver.UnixStreamServer(str(self.directory / "socket"), Handler)
        except BaseException:
            self.temporary.cleanup()
            raise
        self.server.capability = self
        self.active = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}
        )
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.active = False
        self.server.shutdown()  # Wait for the bounded current operation before releasing the lab.
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()
        self.check()

    def check(self):
        if self.error:
            raise InfrastructureError(f"Capability {self.name} failed: {self.error}")

    def grant(self, session, environment):
        if not self.active:
            raise InfrastructureError("Capability must be entered before recipe.run")
        target = f"/capabilities/{self.name}"
        session.extra_mounts.append((self.directory, target, True))
        return self.name, target + "/socket"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        capability = self.server.capability
        self.connection.settimeout(10)
        try:
            raw = self.rfile.readline(REQUEST_LIMIT + 1)
            if len(raw) > REQUEST_LIMIT or not raw.endswith(b"\n"):
                raise ValueError("Capability request too large or incomplete")
            request = json.loads(raw)
            if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
                raise ValueError("Expected operation and arguments")
            if not isinstance(request["operation"], str) or not isinstance(
                request["arguments"], dict
            ):
                raise ValueError("Invalid capability arguments")
            if not capability.active:
                raise ValueError("Capability has closed")
            result = capability.handler(request["operation"], request["arguments"])
            response = {"result": result}
        except (ValueError, OSError) as exc:
            response = {"error": str(exc)[:500]}
        except Exception as exc:
            capability.error = str(exc)
            response = {"error": "Trusted capability infrastructure failed"}
        with contextlib.suppress(OSError):
            self.wfile.write(json.dumps(response).encode() + b"\n")
