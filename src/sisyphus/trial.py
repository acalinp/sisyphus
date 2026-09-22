"""Private host pytest worker. The stdin pipe is a lease on the parent process."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import pytest


def watch_parent(lease: int) -> None:
    # A raw duplicate survives pytest's stdin capture and holds no Python
    # buffered-I/O lock during interpreter shutdown.
    while os.read(lease, 4096):
        pass
    os.kill(os.getpid(), signal.SIGINT)
    # Bound shutdown even if trusted fixture code does not respond to SIGINT.
    time.sleep(2)
    os._exit(130)


def main() -> int:
    threading.Thread(target=watch_parent, args=(os.dup(0),), daemon=True).start()
    return int(pytest.main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
