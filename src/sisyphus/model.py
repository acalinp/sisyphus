"""A bounded Chat Completions editor. Model-requested execution is container-only."""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import Model
from .errors import InfrastructureError
from .evidence import write_json
from .network import tls_context
from .sandbox import logged_process

CONTEXT_BYTES = 48000
OUTPUT_BYTES = 12000
RESPONSE_BYTES = 256000


def tool(name, description, properties):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    tool(
        "read_file",
        "Read up to 6000 bytes of a sandbox file. Offset is zero-based bytes.",
        {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}},
    ),
    tool(
        "write_file",
        "Replace a sandbox file with UTF-8 text. Parent directory must exist.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
    ),
    tool(
        "shell",
        "Run a shell command in the offline workshop, starting in /recipe. "
        "Use for search, edits, and local builds. Output is bounded; redirect large logs "
        "to /tmp and read selected parts.",
        {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
        },
    ),
    tool(
        "finish",
        "End this repair. Only a fresh host test can accept the result. "
        "Leave a short notebook in message for the next repair.",
        {
            "status": {"type": "string", "enum": ["changed", "blocked"]},
            "message": {"type": "string"},
        },
    ),
]


RESEARCH_TOOLS = [
    tool(
        "search",
        "Search the web; returns a short preview and saved results path.",
        {"query": {"type": "string"}},
    ),
    tool(
        "read_url",
        "Read a public HTTPS page through Jina; save text for on-demand reading.",
        {"url": {"type": "string"}},
    ),
    tool(
        "clone_repo",
        "Fetch a public HTTPS Git repository at a branch, tag, or commit. "
        "Returns a read-only local clone and resolved commit. Use shell for git/rg browsing; "
        "copy build inputs into /recipe/vendor for offline replay.",
        {"url": {"type": "string"}, "ref": {"type": "string"}},
    ),
    tool(
        "download",
        "Fetch exact bytes from a granted HTTPS host. Saves URL and SHA-256. "
        "Use empty sha256 to discover the hash; otherwise it must match.",
        {"url": {"type": "string"}, "sha256": {"type": "string"}},
    ),
]


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class Client:
    """Fixed trusted endpoint; no redirects, proxies, credential files, or response-body errors."""

    def __init__(self, settings: Model):
        self.settings = settings
        self.tools = TOOLS
        self.secret = os.environ.get(settings.api_key_env, "") if settings.api_key_env else ""
        if settings.api_key_env and not self.secret:
            raise InfrastructureError(f"Set host environment variable {settings.api_key_env}")
        if any(ord(c) < 32 or ord(c) > 126 for c in self.secret):
            raise InfrastructureError("API credential contains invalid header characters")

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self.secret, "[REDACTED]") if self.secret else value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        return value

    def complete(self, messages: list, deadline: float) -> dict:
        url = urlsplit(self.settings.base_url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InfrastructureError("Model repair exceeded its time budget")
        connection_type = (
            http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        )
        connection_options = {"timeout": min(remaining, 120)}
        if url.scheme == "https":
            connection_options["context"] = tls_context()
        connection = connection_type(url.hostname, url.port, **connection_options)
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if self.settings.provider == "openrouter":
            headers["X-OpenRouter-Title"] = "Sisyphus"
        body = encoded(
            {
                "model": self.settings.model,
                "messages": messages,
                "tools": self.tools,
                "tool_choice": "auto",
                "max_tokens": 4096,
                "stream": False,
            }
        )
        try:
            connection.request(
                "POST", url.path.rstrip("/") + "/chat/completions", body=body, headers=headers
            )
            response = connection.getresponse()
            if response.status != 200:
                raise InfrastructureError(f"Model endpoint returned HTTP {response.status}")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InfrastructureError("Model repair exceeded its time budget")
                if connection.sock is not None:
                    connection.sock.settimeout(min(remaining, 120))
                block = response.read1(min(65536, RESPONSE_BYTES + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if size > RESPONSE_BYTES:
                    raise InfrastructureError("Model response exceeds 256000 bytes")
            document = self.redact(json.loads(b"".join(chunks)))
            choice = document["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError
            # Preserve provider reasoning fields when present, but never arbitrary response keys.
            clean = {"role": "assistant", "content": message.get("content")}
            for key in ("tool_calls", "reasoning_content"):
                if message.get(key) is not None:
                    clean[key] = message[key]
            if clean["content"] is not None and not isinstance(clean["content"], str):
                raise ValueError
            return {
                "message": clean,
                "usage": document.get("usage"),
                "finish_reason": choice.get("finish_reason"),
            }
        except (OSError, http.client.HTTPException) as exc:
            raise InfrastructureError("Model endpoint request failed or timed out") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InfrastructureError("Model endpoint returned an invalid completion") from exc
        finally:
            connection.close()


def conversation(prefix: list, turns: list) -> list:
    """Evict whole exchanges, never orphaning tool results. No model-generated summaries."""
    while turns and len(encoded(prefix + sum(turns, []))) > CONTEXT_BYTES:
        turns.pop(0)
    result = prefix + sum(turns, [])
    if len(encoded(result)) > CONTEXT_BYTES:
        raise InfrastructureError("Initial model context exceeds 48000 bytes")
    return result


def text_arg(args: dict, key: str, limit: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > limit:
        raise ValueError(f"{key} must be text of at most {limit} bytes, without NUL")
    return value


def tool_command(name: str, args: dict, directory: Path):
    """Return sandbox argv and optional stdin. Never interpret a model path on the host."""
    stdin = None
    timeout = 30
    if name in {"read_file", "write_file"}:
        path = text_arg(args, "path", 4096)
        if not path.startswith("/"):
            raise ValueError("Use an absolute sandbox path, e.g. /recipe/build.sh")
        if name == "read_file":
            offset = args.get("offset")
            if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 2**40:
                raise ValueError("offset must be a nonnegative byte offset, at most 2**40")
            command = [
                "sh",
                "-c",
                'test -f "$1" || exit 1; tail -c "+$2" -- "$1" | head -c "$3"',
                "read_file",
                path,
                str(offset + 1),
                str(OUTPUT_BYTES // 2 + 1),
            ]
        else:
            content = text_arg(args, "content", 24000)
            stdin = directory / "input.txt"
            stdin.write_text(content, encoding="utf-8")
            command = ["sh", "-c", 'cat > "$1"', "write_file", path]
    elif name == "shell":
        script = text_arg(args, "command", 24000)
        timeout = args.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
            raise ValueError("timeout must be an integer from 1 to 300 seconds")
        command = ["sh", "-c", script]
    else:
        raise ValueError(f"Unknown tool: {name[:80]}")
    return command, stdin, timeout


def run_tool(workshop, name: str, args: dict, directory: Path, deadline: float) -> dict:
    command, stdin, timeout = tool_command(name, args, directory)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InfrastructureError("Model repair exceeded its time budget")
    result = logged_process(
        ["podman", "exec", "--interactive", workshop.name, *command],
        directory,
        min(timeout, remaining),
        input_file=stdin,
    )
    if result.timed_out or result.output_limited:
        # podman exec's client may die while its container command survives. Stop all writers
        # before letting the model issue another command against the same filesystem.
        workshop.backend.control("stop", "--time=0", workshop.name)
        workshop.backend.control("start", workshop.name)
    if result.returncode == 125 and not result.timed_out and not result.output_limited:
        raise InfrastructureError(f"Podman model tool failed; see {directory}")
    output = {
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "output_limited": result.output_limited,
    }
    for stream in ("stdout", "stderr"):
        with (directory / f"{stream}.log").open("rb") as handle:
            data = handle.read(OUTPUT_BYTES // 2 + 1)
        output[stream] = data[: OUTPUT_BYTES // 2].decode("utf-8", errors="replace")
        output[f"{stream}_truncated"] = len(data) > OUTPUT_BYTES // 2
    return output


def repair(workshop, feedback_file: Path, directory: Path) -> dict:
    editor = workshop.run.config.editor
    client = Client(editor.model)
    research = None
    if getattr(workshop.run.config, "research", None):
        from .research import ResearchStore

        research = ResearchStore(workshop.run)
        client.tools = TOOLS + RESEARCH_TOOLS
    deadline = time.monotonic() + editor.timeout
    feedback = json.loads(feedback_file.read_text())
    feedback.pop("instruction", None)  # External command protocol is not the model protocol.
    feedback["research"] = {
        "path": "/research",
        "enabled": research is not None,
        "download_hosts": list(research.settings.download_hosts) if research else [],
    }
    feedback["test_files"] = [
        f"/tests/{path.name}" for path in sorted((workshop.run.path / "context").glob("*.py"))
    ]
    previous = [entry.get("message", "") for entry in workshop.run.data["repairs"][:-1]]
    prefix = [
        {
            "role": "system",
            "content": (workshop.run.path / "editor" / "instructions.md").read_text(),
        },
        {"role": "user", "content": json.dumps({"feedback": feedback, "notebook": previous[-1:]})},
    ]
    prefix = client.redact(prefix)
    turns = []
    for step in range(1, editor.model.max_steps + 1):
        messages = conversation(prefix, turns)
        exchange = directory / f"model-{step:04d}"
        exchange.mkdir()
        # Persist before the request; an interrupted request still spent one of this repair's steps.
        write_json(exchange / "request.json", {"messages": messages, "model": editor.model.model})
        completion = client.complete(messages, deadline)
        write_json(exchange / "response.json", completion)
        message = completion["message"]
        calls = message.get("tool_calls")
        if not calls:
            turns.append(
                [
                    message,
                    {
                        "role": "user",
                        "content": "Use tools to edit /recipe, or call finish to end this repair.",
                    },
                ]
            )
            continue
        if not isinstance(calls, list) or len(calls) > 8:
            raise InfrastructureError("Model must return at most 8 tool calls per response")
        turn = [message]
        identifiers = set()
        for index, call in enumerate(calls):
            try:
                identifier = call["id"]
                name = call["function"]["name"]
                raw = call["function"]["arguments"]
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or identifier in identifiers
                    or not isinstance(name, str)
                    or not isinstance(raw, str)
                ):
                    raise ValueError
                identifiers.add(identifier)
            except (KeyError, TypeError, ValueError) as exc:
                raise InfrastructureError("Invalid model tool call envelope") from exc
            tool_dir = exchange / f"tool-{index + 1:02d}"
            tool_dir.mkdir()
            try:
                args = json.loads(raw)
                schema = next(
                    (
                        t["function"]["parameters"]
                        for t in client.tools
                        if t["function"]["name"] == name
                    ),
                    None,
                )
                if not schema or not isinstance(args, dict) or set(args) != set(schema["required"]):
                    raise ValueError("Use exactly the arguments in the tool schema")
                if name == "finish":
                    status = args.get("status")
                    note = text_arg(args, "message", 2000)
                    if not isinstance(status, str) or status not in {"changed", "blocked"}:
                        raise ValueError("status must be changed or blocked")
                    if len(calls) != 1:
                        raise ValueError("Call finish alone, after all other tools have returned")
                    return {"version": 1, "status": status, "message": note}
                if name in {"search", "read_url", "clone_repo", "download"} and research:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise InfrastructureError("Model repair exceeded its time budget")
                    output = research.call(name, args, timeout=remaining)
                else:
                    output = run_tool(workshop, name, args, tool_dir, deadline)
            except (ValueError, UnicodeError) as exc:
                output = {"error": str(exc)[:500]}
            output = client.redact(output)
            write_json(tool_dir / "result.json", output)
            turn.append(
                {
                    "role": "tool",
                    "tool_call_id": identifier,
                    "content": json.dumps(output, ensure_ascii=False),
                }
            )
        turns.append(turn)
    return {
        "version": 1,
        "status": "changed",
        "message": "Model step budget reached; test the current recipe before another repair.",
    }
