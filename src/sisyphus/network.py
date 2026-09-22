"""Small public-HTTPS boundary shared by downloads and the isolated Git fetcher."""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import os
import select
import socket
import socketserver
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .errors import InfrastructureError


class FetchError(ValueError):
    """A refused or unsuccessful research operation; safe to show the model."""


def public_url(value: str, hosts=None):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value):
        raise FetchError("Expected a public HTTPS URL")
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
            and not url.fragment
            and url.port in (None, 443)
        )
    except ValueError:
        valid = False
    if not valid:
        raise FetchError("Only HTTPS port 443 without embedded credentials is allowed")
    host = url.hostname
    if hosts is not None and host not in hosts:
        raise FetchError(f"Host {host} is not in research.download_hosts")
    public_addresses(host)  # Validate user targets even when a remote reader will fetch them.
    return url


def public_addresses(host: str):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("Cannot resolve download host") from exc
    if not addresses or any(
        not ipaddress.ip_address(item[4][0]).is_global
        or ipaddress.ip_address(item[4][0]).is_multicast
        for item in addresses
    ):
        raise FetchError("Private, loopback, link-local, and non-public destinations are denied")
    return addresses


def public_socket(host: str, timeout: float):
    # Connect to the address that was checked, never resolve the name again in connect().
    for family, kind, protocol, _canonical, address in public_addresses(host):
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(timeout)
        try:
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise FetchError("Cannot connect to public download host")


def tls_context():
    context = ssl.create_default_context()
    # Standalone Python distributions may have no default cafile on Debian/Fedora.
    # Add the installed OS bundle, preserving certificate/hostname checks and explicit overrides.
    if not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR"):
        for file in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(file).is_file():
                context.load_verify_locations(cafile=file)
                break
    return context


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        raw = public_socket(self.host, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch(
    url: str, output, *, hosts=None, headers=None, limit=8 * 1024**2, timeout=60, redirects=0
):
    deadline = time.monotonic() + timeout
    request_headers = dict(headers or {})
    for hop in range(redirects + 1):
        parsed = public_url(url, hosts)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("Download timed out")
        connection = PublicHTTPS(parsed.hostname, timeout=remaining, context=tls_context())
        try:
            connection.request(
                "GET",
                (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                headers=request_headers,
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308) and hop < redirects:
                location = response.getheader("Location")
                if not location:
                    raise FetchError("Redirect has no Location")
                next_url = urljoin(url, location)
                next_parsed = public_url(next_url, hosts)
                if next_parsed.hostname != parsed.hostname:
                    request_headers.pop("Authorization", None)
                url = next_url
                continue
            if response.status != 200:
                raise FetchError(f"Download returned HTTP {response.status}")
            size = 0
            while True:
                if time.monotonic() >= deadline:
                    raise FetchError("Download timed out")
                if connection.sock:
                    connection.sock.settimeout(max(0.1, deadline - time.monotonic()))
                block = response.read1(min(65536, limit + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise FetchError("Download exceeds its byte budget")
                output.write(block)
            return {"bytes": size, "final_url": url}
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError("HTTPS download failed or timed out") from exc
        finally:
            connection.close()
    raise FetchError("Too many redirects")


class Tunnel(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """CONNECT-only Unix proxy. The fetch container has no other network interface."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, path, hosts, *, limit, timeout):
        self.hosts, self.limit = hosts, limit
        self.deadline = time.monotonic() + timeout
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(str(path), TunnelHandler)
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1})

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopping.set()
        self.shutdown()
        self.server_close()
        self.thread.join()


class TunnelHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        if not server.slots.acquire(blocking=False):
            return
        try:
            self.connection.settimeout(5)
            line = self.rfile.readline(8193)
            method, authority, version = line.decode("ascii").strip().split(" ")
            if method != "CONNECT" or version != "HTTP/1.1" or not authority.endswith(":443"):
                raise FetchError("Only CONNECT host:443 is allowed")
            host = authority[:-4]
            if host not in server.hosts:
                raise FetchError("Host not granted")
            count = len(line)
            while line := self.rfile.readline(8193):
                count += len(line)
                if count > 8192:
                    raise FetchError("Proxy headers too large")
                if line == b"\r\n":
                    break
            with public_socket(host, 10) as remote:
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                sockets = [self.connection, remote]
                while not server.stopping.is_set() and time.monotonic() < server.deadline:
                    ready, _, _ = select.select(sockets, [], [], 0.1)
                    for incoming in ready:
                        block = incoming.recv(65536)
                        if not block:
                            return
                        with server.lock:
                            server.limit -= len(block)
                            if server.limit < 0:
                                return
                        (remote if incoming is self.connection else self.connection).sendall(block)
        except (OSError, ValueError, InfrastructureError):
            with contextlib.suppress(OSError):
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
        finally:
            server.slots.release()
