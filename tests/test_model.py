import io
import json
import time
from types import SimpleNamespace

import pytest

from sisyphus.config import Config, Model, Research
from sisyphus.errors import InfrastructureError
from sisyphus.model import (
    CONTEXT_BYTES,
    RESPONSE_BYTES,
    Client,
    conversation,
    encoded,
    repair,
    tool_command,
)


def settings(**kwargs):
    return Model.load({"model": "local-model", "base_url": "http://localhost:8080/v1", **kwargs})


@pytest.mark.parametrize(
    "override",
    [
        {"model": ""},
        {"base_url": "file:///tmp/socket"},
        {"base_url": "https://user:password@example.test/v1"},
        {"base_url": "http://localhost:wrong/v1"},
        {"base_url": "http://localhost/v1?secret=x"},
        {"base_url": "http://localhost/v1#fragment"},
        {"base_url": "http://local\nhost/v1"},
        {"api_key_env": "SECRET"},
        {"api_key_env": "a=b"},
        {"provider": "unknown"},
        {"max_steps": True},
        {"max_steps": 0},
        {"max_steps": 101},
        {"command": ["sh", "edit"]},
        {"path": "editor"},
        {"api_key": "never-store-me"},
    ],
)
def test_model_configuration_rejects_unsafe_or_ambiguous_settings(override):
    with pytest.raises(InfrastructureError):
        settings(**override)


def test_model_configuration_round_trips_without_command_directory(project, monkeypatch):
    config = project()
    monkeypatch.setenv("PROVIDER_KEY", "do-not-store-this")
    with config.file.open("a") as handle:
        handle.write(
            '[editor]\nmodel="local"\nbase_url="https://example.test/v1"\n'
            'api_key_env="PROVIDER_KEY"\n'
        )
    config = Config.load(config.file)
    assert config.editor.path is None
    assert Config.restore(config.record()) == config
    assert "do-not-store-this" not in json.dumps(config.record())


def test_missing_host_credential_is_actionable(monkeypatch):
    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    with pytest.raises(InfrastructureError, match="Set host environment variable"):
        Client(settings(base_url="https://example.test/v1", api_key_env="TEST_PROVIDER_KEY"))


def test_openrouter_defaults_and_request_headers(monkeypatch):
    secret = "not-a-real-openrouter-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    context = object()
    connection_options = {}
    connection = Connection(
        encoded(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "ready"}}
                ]
            }
        )
    )
    monkeypatch.setattr("sisyphus.model.tls_context", lambda: context)

    def connect(*args, **kwargs):
        connection_options.update(kwargs)
        return connection

    monkeypatch.setattr("sisyphus.model.http.client.HTTPSConnection", connect)
    model = Model.load({"provider": "openrouter", "model": "openai/gpt-5.2"})
    result = Client(model).complete([], time.monotonic() + 10)
    method, path, body, headers = connection.request_data
    assert model.base_url == "https://openrouter.ai/api/v1"
    assert model.api_key_env == "OPENROUTER_API_KEY"
    assert (method, path) == ("POST", "/api/v1/chat/completions")
    assert body["model"] == "openai/gpt-5.2"
    assert headers["Authorization"] == "Bearer " + secret
    assert headers["X-OpenRouter-Title"] == "Sisyphus"
    assert connection_options["context"] is context
    assert result["message"]["content"] == "ready"


class Connection:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
        self.sock = None
        self.closed = False

    def request(self, method, path, *, body, headers):
        self.request_data = method, path, json.loads(body), headers

    def getresponse(self):
        return SimpleNamespace(status=self.status, read1=io.BytesIO(self.body).read1)

    def close(self):
        self.closed = True


def test_transport_secret_stays_in_header_and_echoes_are_redacted(monkeypatch):
    secret = "not-a-real-test-key"
    monkeypatch.setenv("TEST_PROVIDER_KEY", secret)
    connection = Connection(
        encoded(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": secret,
                            "reasoning_content": "reasoning",
                            "tool_calls": [
                                {"id": "1", "function": {"name": "shell", "arguments": secret}}
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens": 3},
            }
        )
    )
    monkeypatch.setattr("sisyphus.model.http.client.HTTPSConnection", lambda *a, **k: connection)
    client = Client(settings(base_url="https://example.test/v1", api_key_env="TEST_PROVIDER_KEY"))
    result = client.complete([{"role": "user", "content": "fix recipe"}], time.monotonic() + 10)
    method, path, body, headers = connection.request_data
    assert (method, path) == ("POST", "/v1/chat/completions")
    assert secret not in json.dumps(body)
    assert headers["Authorization"] == "Bearer " + secret
    assert secret not in json.dumps(result)
    assert result["message"]["reasoning_content"] == "reasoning"
    assert connection.closed


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (302, b"secret location", "HTTP 302"),
        (500, b"sensitive upstream diagnostic", "HTTP 500"),
        (200, b"not json", "invalid completion"),
        (200, b"{}", "invalid completion"),
        (200, b"x" * (RESPONSE_BYTES + 1), "exceeds"),
    ],
)
def test_transport_bounds_and_hides_provider_errors(monkeypatch, status, body, error):
    connection = Connection(body, status)
    monkeypatch.setattr("sisyphus.model.http.client.HTTPConnection", lambda *a, **k: connection)
    with pytest.raises(InfrastructureError, match=error) as exc:
        Client(settings()).complete([], time.monotonic() + 10)
    assert "sensitive" not in str(exc.value)
    assert connection.closed
    assert "Authorization" not in connection.request_data[3]


def test_context_evicts_entire_exchanges():
    prefix = [{"role": "system", "content": "policy"}]
    turns = [
        [
            {"role": "assistant", "tool_calls": [{"id": "old"}]},
            {"role": "tool", "tool_call_id": "old", "content": "x" * CONTEXT_BYTES},
        ],
        [{"role": "assistant", "content": "new"}, {"role": "user", "content": "continue"}],
    ]
    result = conversation(prefix, turns)
    assert len(encoded(result)) <= CONTEXT_BYTES
    assert len(turns) == 1
    assert "old" not in json.dumps(result)
    assert result[0] == prefix[0]


def test_path_arguments_are_not_interpreted_on_host(tmp_path):
    path = '/recipe/a; $(touch /host-pwned) `id` "quotes"'
    command, stdin, timeout = tool_command(
        "write_file", {"path": path, "content": "hello"}, tmp_path
    )
    assert command[-1] == path
    assert stdin.read_text() == "hello"
    assert stdin.parent == tmp_path
    assert len(list(tmp_path.iterdir())) == 1


def completion(name, args):
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
            ],
        },
        "usage": {"completion_tokens": 1},
    }


@pytest.mark.parametrize("with_research", [False, True])
def test_repair_recovers_invalid_tool_args_and_preserves_notebook(
    tmp_path, monkeypatch, with_research
):
    (tmp_path / "editor").mkdir()
    (tmp_path / "editor" / "instructions.md").write_text("trusted instructions")
    feedback = tmp_path / "feedback.json"
    feedback.write_text('{"instruction":"external protocol", "outcome":"reject"}')
    directory = tmp_path / "repair"
    directory.mkdir()
    workshop = SimpleNamespace(
        run=SimpleNamespace(
            path=tmp_path,
            config=SimpleNamespace(editor=SimpleNamespace(model=settings(max_steps=3), timeout=10)),
            data={"repairs": [{"message": "Previous useful finding"}, {}]},
        )
    )
    if with_research:
        workshop.run.config.research = Research(download_hosts=("allowed.example",))
    responses = [
        completion("shell", {"command": "pwd", "timeout": False}),
        completion("finish", {"status": "changed", "message": "fixed it"}),
    ]
    seen = []

    def complete(self, messages, deadline):
        seen.append(encoded(messages).decode())
        return responses.pop(0)

    monkeypatch.setattr(Client, "complete", complete)
    result = repair(workshop, feedback, directory)
    assert result["status"] == "changed"
    assert ("allowed.example" in seen[0]) == with_research
    assert "Previous useful finding" in seen[0]
    assert "external protocol" not in seen[0]
    assert "timeout must be an integer" in seen[1]
    assert (directory / "model-0002" / "response.json").is_file()


def test_model_step_budget_requests_test_not_acceptance(tmp_path, monkeypatch):
    (tmp_path / "editor").mkdir()
    (tmp_path / "editor" / "instructions.md").write_text("policy")
    (tmp_path / "feedback.json").write_text("{}")
    directory = tmp_path / "repair"
    directory.mkdir()
    workshop = SimpleNamespace(
        run=SimpleNamespace(
            path=tmp_path,
            data={"repairs": [{}]},
            config=SimpleNamespace(editor=SimpleNamespace(model=settings(max_steps=2), timeout=10)),
        )
    )
    monkeypatch.setattr(
        Client,
        "complete",
        lambda *args: {
            "message": {"role": "assistant", "content": "I think I passed!"},
        },
    )
    result = repair(workshop, tmp_path / "feedback.json", directory)
    assert result["status"] == "changed"
    assert "step budget" in result["message"]
    assert len(list(directory.glob("model-*"))) == 2
