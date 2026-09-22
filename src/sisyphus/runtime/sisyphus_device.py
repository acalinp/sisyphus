"""Small client mounted read-only at /sisyphus for explicitly granted recipe invocations."""

import json
import os
import socket


def call(name, operation, **arguments):
    endpoint = json.loads(os.environ["SISYPHUS_CAPABILITIES"])[name]
    data = json.dumps({"operation": operation, "arguments": arguments}).encode() + b"\n"
    if len(data) > 12 * 1024**2:
        raise ValueError("Capability request too large")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(90)
        connection.connect(endpoint)
        connection.sendall(data)
        with connection.makefile("rb") as incoming:
            raw = incoming.readline(65537)
        if len(raw) > 65536:
            raise RuntimeError("Capability reply too large")
        response = json.loads(raw)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]
