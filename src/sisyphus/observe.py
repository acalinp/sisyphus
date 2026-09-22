"""Trusted Linux serial capture with bounded storage and explicit observation lifetime."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import termios
import threading
import time
import tty

from .errors import InfrastructureError


class SerialLog:
    def __init__(self, device, directory, *, baud=115200, max_bytes=16 * 1024**2):
        self.device, self.directory, self.baud = device, directory, baud
        self.max_bytes = max_bytes
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self.buffer = bytearray()
        self.error = None
        self.position = 0
        self.buffer_start = 0

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd, termios.TCSANOW)
            settings = termios.tcgetattr(self.fd)
            speed = getattr(termios, f"B{self.baud}")
            settings[4] = settings[5] = speed
            settings[2] |= termios.CLOCAL | termios.CREAD
            settings[2] &= ~getattr(termios, "CRTSCTS", 0)
            termios.tcsetattr(self.fd, termios.TCSANOW, settings)
            termios.tcflush(self.fd, termios.TCIFLUSH)
            self.raw = (self.directory / "serial.log").open("xb")
            self.events = (self.directory / "serial.events.jsonl").open("x")
        except BaseException:
            if hasattr(self, "raw"):
                self.raw.close()
            if hasattr(self, "saved"):
                with contextlib.suppress(OSError, termios.error):
                    termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
            os.close(self.fd)
            raise
        self.thread = threading.Thread(target=self._read)
        self.thread.start()
        return self

    def _read(self):
        try:
            while not self.stop.is_set():
                ready, _, _ = select.select([self.fd], [], [], 0.1)
                if not ready:
                    continue
                try:
                    block = os.read(self.fd, 65536)
                except BlockingIOError:
                    continue
                if not block:
                    raise InfrastructureError("Serial device disconnected")
                if self.position + len(block) > self.max_bytes:
                    raise InfrastructureError("Serial capture exceeded its byte budget")
                self.raw.write(block)
                self.raw.flush()
                self.events.write(
                    json.dumps(
                        {"time_ns": time.time_ns(), "offset": self.position, "bytes": len(block)}
                    )
                    + "\n"
                )
                self.events.flush()
                with self.condition:
                    self.position += len(block)
                    self.buffer.extend(block)
                    if len(self.buffer) > 1024**2:
                        trim = len(self.buffer) - 1024**2
                        del self.buffer[:trim]
                        self.buffer_start += trim
                    self.condition.notify_all()
        except Exception as exc:
            self.error = str(exc)
            with self.condition:
                self.condition.notify_all()

    def check(self):
        if self.error:
            raise InfrastructureError(self.error)

    def mark(self):
        self.check()
        with self.condition:
            return self.position

    def contains(self, text, *, after=0, timeout=0):
        text = text.encode() if isinstance(text, str) else text
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                self.check()
                if after < self.buffer_start:
                    raise InfrastructureError(
                        "Requested serial window exceeds in-memory history; "
                        "inspect the retained serial log"
                    )
                if text in self.buffer[after - self.buffer_start :]:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)

    def tail(self):
        """A bounded, non-consuming console view for a granted transport client."""
        self.check()
        with self.condition:
            return bytes(self.buffer[-4096:]).decode(errors="replace")

    def write(self, data):
        self.check()
        if not isinstance(data, bytes) or len(data) > 1024:
            raise ValueError("Serial writes are limited to 1024 bytes")
        deadline = time.monotonic() + 2
        while data:
            if time.monotonic() > deadline:
                raise InfrastructureError("Serial write timed out")
            if select.select([], [self.fd], [], 0.1)[1]:
                try:
                    data = data[os.write(self.fd, data) :]
                except BlockingIOError:
                    pass

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.raw.close()
        self.events.close()
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        os.close(self.fd)
        self.check()
