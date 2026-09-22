"""Executed only inside the disposable offline Git fetch container."""

import json
import os
import resource
import select
import signal
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path


class Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as upstream:
            upstream.connect("/broker/socket")
            peers = [self.request, upstream]
            while True:
                readable, _, _ = select.select(peers, [], [], 30)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.request else self.request).sendall(data)


# Give the fetch helper and Git children one private process group for budget cleanup.
try:
    os.setsid()
except PermissionError:
    if os.getpgrp() != os.getpid():
        raise

request = json.loads(Path("/request.json").read_text())
resource.setrlimit(resource.RLIMIT_FSIZE, (request["max_bytes"], request["max_bytes"]))


def monitor():
    while True:
        size = count = 0
        try:
            for root, directories, files in os.walk("/recipe", followlinks=False):
                count += len(files) + len(directories)
                for name in files:
                    try:
                        size += os.lstat(os.path.join(root, name)).st_size
                    except FileNotFoundError:
                        pass
                if size > request["max_bytes"] or count > request["max_files"]:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
        except OSError:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        time.sleep(0.1)


threading.Thread(target=monitor, daemon=True).start()
server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Bridge)
server.daemon_threads = True
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
os.makedirs("/empty", exist_ok=True)
environment = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/empty",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
}
command = [
    "git",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    f"http.proxy=http://127.0.0.1:{server.server_address[1]}",
    "-c",
    "http.followRedirects=false",
    "-c",
    "init.templateDir=/empty",
    "-c",
    "core.hooksPath=/empty",
]


def git(*args, capture=False):
    return subprocess.run(
        command + list(args),
        cwd="/recipe",
        env=environment,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        text=True,
    ).stdout


try:
    git("init", "repo")
    git("-C", "repo", "fetch", "--depth=1", "--no-tags", "--", request["url"], request["ref"])
    commit = git("-C", "repo", "rev-parse", "--verify", "FETCH_HEAD^{commit}", capture=True).strip()
    git("-C", "repo", "checkout", "--detach", commit)
    Path("/recipe/commit.txt").write_text(commit + "\n")
finally:
    server.shutdown()
    server.server_close()
