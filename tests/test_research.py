import hashlib
import io
import json
import socket
from types import SimpleNamespace

import pytest

from sisyphus.config import Config, Limits, Research
from sisyphus.network import FetchError, fetch, public_addresses, public_socket, public_url
from sisyphus.research import ResearchStore
from sisyphus.snapshot import snapshot


def addresses(*values):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443)) for value in values]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.134.6.235",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "192.168.1.1",
        "::1",
        "fe80::1",
    ],
)
def test_nonpublic_dns_is_denied(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses(address))
    with pytest.raises(FetchError):
        public_addresses("example.test")


def test_mixed_public_and_private_dns_is_denied(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8", "127.0.0.1"))
    with pytest.raises(FetchError):
        public_url("https://example.test/a")


def test_connect_uses_the_checked_address(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))
    connection = SimpleNamespace(settimeout=lambda t: None, connect=calls.append)
    monkeypatch.setattr(socket, "socket", lambda *a: connection)
    assert public_socket("example.test", 1) is connection
    assert calls == [("8.8.8.8", 443)]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://example.test/a",
        "https://user:key@example.test/a",
        "https://example.test:22/a",
        "https://example.test/a#fragment",
        "https://example.test/\nx",
    ],
)
def test_non_https_or_credential_urls_are_denied(url):
    with pytest.raises(FetchError):
        public_url(url)


class Response:
    def __init__(self, status=200, body=b"hello", location=None):
        self.status, self.read1, self.location = status, io.BytesIO(body).read1, location

    def getheader(self, name):
        return self.location


def mock_https(monkeypatch, responses):
    requests = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))

    class Connection:
        sock = None

        def __init__(self, host, **kwargs):
            self.host = host

        def request(self, method, path, headers):
            requests.append((self.host, path, dict(headers)))

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr("sisyphus.network.PublicHTTPS", Connection)
    return requests


def test_redirect_checks_host_and_drops_credentials(monkeypatch):
    requests = mock_https(
        monkeypatch, [Response(302, location="https://second.test/file"), Response()]
    )
    output = io.BytesIO()
    result = fetch(
        "https://first.test/start",
        output,
        hosts=["first.test", "second.test"],
        headers={"Authorization": "secret"},
        redirects=3,
    )
    assert output.getvalue() == b"hello"
    assert result["final_url"] == "https://second.test/file"
    assert requests[0][2]["Authorization"] == "secret"
    assert "Authorization" not in requests[1][2]


def test_redirect_cannot_leave_granted_hosts(monkeypatch):
    requests = mock_https(monkeypatch, [Response(302, location="https://forbidden.test/file")])
    with pytest.raises(FetchError, match="download_hosts"):
        fetch("https://first.test/start", io.BytesIO(), hosts=["first.test"], redirects=3)
    assert len(requests) == 1


def test_redirect_rechecks_private_dns(monkeypatch):
    mock_https(monkeypatch, [Response(302, location="https://private.test/file")])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **k: addresses("127.0.0.1" if host == "private.test" else "8.8.8.8"),
    )
    with pytest.raises(FetchError, match="Private"):
        fetch("https://first.test/start", io.BytesIO(), redirects=3)


def test_download_is_bounded(monkeypatch):
    mock_https(monkeypatch, [Response(body=b"long data")])
    output = io.BytesIO()
    with pytest.raises(FetchError, match="byte budget"):
        fetch("https://example.test/", output, limit=3)
    assert len(output.getvalue()) <= 3


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))
    run = SimpleNamespace(
        path=tmp_path,
        data={},
        save=lambda: None,
        config=SimpleNamespace(
            research=Research(download_hosts=("example.test",)), limits=Limits()
        ),
    )
    return ResearchStore(run)


def test_download_provenance_and_checksum(store, monkeypatch):
    def download(url, output, **kwargs):
        output.write(b"source archive bytes")
        return {"final_url": url, "bytes": 20}

    monkeypatch.setattr("sisyphus.research.fetch", download)
    result = store.call("download", {"url": "https://example.test/source", "sha256": ""})
    assert result["sha256"] == hashlib.sha256(b"source archive bytes").hexdigest()
    assert result["path"].startswith("/research/")
    assert len(store.run.data["research_items"]) == 1
    with pytest.raises(FetchError, match="SHA-256"):
        store.call("download", {"url": "https://example.test/source", "sha256": "0" * 64})
    assert len(store.run.data["research_items"]) == 1
    assert not list(store.root.glob(".incoming-*"))


def test_jina_secret_is_header_only_and_full_page_stays_on_disk(store, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", "test-provider-secret")

    def jina(url, output, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer test-provider-secret"
        output.write(
            json.dumps(
                {
                    "data": {
                        "title": "Doc",
                        "content": "test-provider-secret " + "large page " * 10000,
                    }
                }
            ).encode()
        )

    monkeypatch.setattr("sisyphus.research.fetch", jina)
    result = store.call("read_url", {"url": "https://example.test/doc"})
    assert len(json.dumps(result)) < 4000
    assert "test-provider-secret" not in json.dumps(result)
    saved = next(store.root.glob("*/document.md")).read_text()
    assert len(saved) > 100000
    assert "test-provider-secret" not in saved


def test_missing_jina_search_key_is_actionable(store, monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    with pytest.raises(FetchError, match="JINA_API_KEY"):
        store.call("search", {"query": "PocketBeagle DFU"})


def test_config_research_and_limits_round_trip(project):
    config = project()
    with config.file.open("a") as output:
        output.write('[research]\ndownload_hosts=["github.com"]\n[limits]\nmemory_mb=4096\n')
    config = Config.load(config.file)
    assert config.research.download_hosts == ("github.com",)
    assert Config.restore(config.record()) == config


def test_contained_links_survive_snapshot_and_affect_identity(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "file").write_text("content")
    (root / "link").symlink_to("file")
    first = snapshot(root, tmp_path / "saved")
    assert (tmp_path / "saved/link").is_symlink()
    assert (tmp_path / "saved/link").read_text() == "content"
    assert first == snapshot(tmp_path / "saved")
    (root / "link").unlink()
    (root / "link").symlink_to("./file")
    assert first != snapshot(root)


def test_links_cannot_escape_through_other_links(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a").symlink_to("b/secret")
    (root / "b").symlink_to("../outside")
    with pytest.raises(Exception, match="unsupported"):
        snapshot(root, tmp_path / "copy")


def test_snapshot_file_and_byte_limits(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "one").write_bytes(b"a" * (1024**2 + 1))
    with pytest.raises(Exception, match="byte limit"):
        snapshot(root, limits=Limits(snapshot_mb=1))
    (root / "two").write_text("b")
    with pytest.raises(Exception, match="file-count"):
        snapshot(root, limits=Limits(snapshot_files=1))


def test_saved_research_cannot_change_on_resume(project, tmp_path, monkeypatch):
    from sisyphus.state import Run

    config = project()
    with config.file.open("a") as handle:
        handle.write(
            '[editor]\nmodel="local"\nbase_url="http://localhost:8080/v1"\n'
            '[research]\ndownload_hosts=["example.test"]\n'
        )
    (tmp_path / "test_goal.py").write_text("def test_goal(recipe): pass\n")
    backend = SimpleNamespace(resolve=lambda image: "a" * 64)
    run = Run.create(Config.load(config.file), str(tmp_path / "test_goal.py"), 2, 30, backend)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))

    def download(url, output, **kwargs):
        output.write(b"original")
        return {"bytes": 8, "final_url": url}

    monkeypatch.setattr("sisyphus.research.fetch", download)
    store = ResearchStore(run)
    store.call("download", {"url": "https://example.test/source", "sha256": ""})
    run.verify_trusted()
    next(store.root.glob("*/content")).write_bytes(b"changed")
    with pytest.raises(Exception, match="Saved research input changed"):
        Run.load(run.path).verify_trusted()


def test_directory_swap_to_link_cannot_read_outside_snapshot(tmp_path, monkeypatch):
    import os

    root = tmp_path / "source"
    (root / "child").mkdir(parents=True)
    (root / "child/data").write_text("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("host secret")
    original_open = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "child" and flags & os.O_DIRECTORY:
            (root / "child").rename(root / "old-child")
            (root / "child").symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(Exception, match="Cannot snapshot"):
        snapshot(root, tmp_path / "copy")
    assert not list((tmp_path / "copy").rglob("data"))
