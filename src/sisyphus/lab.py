"""A deliberately small trusted DFU/serial fixture, scoped to one physical USB port."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from .capability import Capability
from .config import argv
from .errors import InfrastructureError
from .evidence import write_json
from .observe import SerialLog
from .sandbox import logged_process


class DeviceLease:
    def __init__(self, identity):
        self.identity = identity

    def __enter__(self):
        root = Path(tempfile.gettempdir()) / f"sisyphus-lab-{os.getuid()}"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise InfrastructureError("Lab lock directory must be private and owned by this user")
        name = hashlib.sha256(self.identity.encode()).hexdigest()
        self.fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            raise InfrastructureError("Another test owns this device") from exc
        return self

    def __exit__(self, *args):
        os.close(self.fd)


class DFUBoard:
    def __init__(self, *, usb_port, serial_device, reset_command=None, dfu_id="0451:6165"):
        if not re.fullmatch(r"[0-9]+-[0-9]+(?:\.[0-9]+)*", usb_port):
            raise InfrastructureError("usb_port must be a physical sysfs port such as 1-2.3")
        if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", dfu_id):
            raise InfrastructureError("dfu_id must be lowercase vendor:product hex")
        self.usb_port, self.serial_device, self.dfu_id = usb_port, serial_device, dfu_id
        self.reset_command = argv(reset_command, "lab reset command") if reset_command else None
        self.number = 0
        self.sysfs = Path("/sys/bus/usb/devices")

    @contextlib.contextmanager
    def record(self, evidence):
        self.directory = evidence.path / "lab"
        self.directory.mkdir()
        instructions = self.directory / "board-access.md"
        instructions.write_text(BOARD_ACCESS)
        evidence.attach(instructions, name="board-access.md", description="Granted board API")
        with (
            DeviceLease("usb:" + self.usb_port),
            DeviceLease("serial:" + str(Path(self.serial_device).resolve())),
        ):
            try:
                with SerialLog(self.serial_device, self.directory) as serial:
                    self.serial = serial
                    with Capability("board", self.dispatch) as access:
                        self.access = access
                        yield self
            finally:
                for name in ("serial.log", "serial.events.jsonl"):
                    if (self.directory / name).is_file():
                        evidence.attach(
                            self.directory / name,
                            name=name,
                            description="Trusted serial capture from selected board",
                        )

    def identity(self):
        directory = self.sysfs / self.usb_port
        try:
            return (
                (directory / "idVendor").read_text().strip()
                + ":"
                + (directory / "idProduct").read_text().strip()
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise InfrastructureError("Cannot inspect selected USB port") from exc

    def command(self, args, timeout=15):
        self.number += 1
        directory = self.directory / f"command-{self.number:04d}"
        directory.mkdir()
        write_json(directory / "request.json", {"argv": list(args), "time_ns": time.time_ns()})
        result = logged_process(list(args), directory, timeout)
        write_json(
            directory / "result.json",
            {
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "output_limited": result.output_limited,
            },
        )
        if result.output_limited:
            raise InfrastructureError("Lab command exceeded output budget")
        text = "\n".join(
            (directory / f"{s}.log").read_text(errors="replace") for s in ("stdout", "stderr")
        )
        return result.returncode, text, result.timed_out

    def reset_into_dfu(self, timeout=30):
        if self.reset_command:
            status, _text, timed_out = self.command(self.reset_command)
            if status or timed_out:
                raise InfrastructureError("Trusted board reset command failed")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.serial.check()
            if self.identity() == self.dfu_id:
                status, text, timed_out = self.command(
                    ["dfu-util", "-p", self.usb_port, "-d", self.dfu_id, "-l"], timeout=5
                )
                if not status and not timed_out and "Found DFU:" in text:
                    return
            time.sleep(0.1)
        raise InfrastructureError(
            "Selected board did not reach ROM DFU baseline; reset into USB DFU"
        )

    def dispatch(self, operation, arguments):
        if operation == "serial_read" and not arguments:
            return {"text": self.serial.tail()}
        if operation == "serial_write":
            if set(arguments) != {"text"} or not isinstance(arguments["text"], str):
                raise ValueError("serial_write requires text")
            self.serial.write(arguments["text"].encode())
            return {"sent": True}
        if operation == "dfu_list" and not arguments:
            status, text, timed_out = self.command(["dfu-util", "-p", self.usb_port, "-l"])
            return {"returncode": status, "text": text[-16384:], "timed_out": timed_out}
        if (
            operation != "dfu_download"
            or not {"alternate", "data"} <= set(arguments)
            or set(arguments) - {"alternate", "data", "reset"}
        ):
            raise ValueError(
                "Granted operations: dfu_list, dfu_download, serial_read, serial_write"
            )
        alternate = arguments["alternate"]
        reset = arguments.get("reset", False)
        if (
            not isinstance(alternate, str)
            or not 1 <= len(alternate) <= 128
            or alternate.startswith("-")
            or any(ord(c) < 32 for c in alternate)
            or not isinstance(reset, bool)
        ):
            raise ValueError("alternate must be a DFU name/index; reset must be boolean")
        try:
            data = base64.b64decode(arguments["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("DFU payload must be base64") from exc
        if not 0 < len(data) <= 8 * 1024**2:
            raise ValueError("DFU payload must be 1 byte to 8 MiB")
        # Transport only: the agent chooses payloads, alternates, ordering, and reset behavior.
        payload = self.directory / f"payload-{self.number}.bin"
        payload.write_bytes(data)
        write_json(
            payload.with_suffix(".json"),
            {
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "alternate": alternate,
            },
        )
        status, text, timed_out = self.command(
            [
                "dfu-util",
                "-p",
                self.usb_port,
                *(["-R"] if reset else []),
                "-a",
                alternate,
                "-D",
                str(payload),
            ],
            timeout=30,
        )
        return {"returncode": status, "text": text[-16384:], "timed_out": timed_out}

    def fastboot_version(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.serial.check()
            status, text, timed_out = self.command(
                ["fastboot", "-s", "usb:" + self.usb_port, "getvar", "version-bootloader"],
                timeout=min(3, max(0.1, deadline - time.monotonic())),
            )
            if not status and not timed_out:
                match = re.search(r"version-bootloader:\s*([^\r\n]+)", text)
                if match:
                    return match.group(1)
        return None


BOARD_ACCESS = """# Board access during recipe.run

Only the selected physical USB port and its serial console are granted. The workshop
has no hardware access. In replay, Python 3 can use the mounted client:

    import sys
    sys.path.insert(0, "/sisyphus")
    from sisyphus_device import call
    result = call("board", "dfu_list")

Operations (keyword arguments after the operation name):
- dfu_list(): return dfu-util's current descriptors as text, returncode, timed_out.
- dfu_download(alternate, data, reset=False): alternate name/index, base64 payload
  (1 byte to 8 MiB), optional USB reset after transfer. Return text, returncode, timed_out.
  Discover descriptors/re-enumeration yourself. A transfer response does not prove boot.
- serial_read(): return text, the latest 4096 serial bytes decoded as UTF-8. Nonblocking;
  repeated reads can overlap. Poll in your recipe when needed.
- serial_write(text): send at most 1024 UTF-8 bytes.

The host captures full bounded serial logs and transport commands outside the recipe.
Serial logs appear in the evidence index after each trial, including failed trials.
Transport requests, responses, and payload hashes are in the sibling lab/ directory
beside attachments/ in that same trial's evidence; read them when needed.
Save prepared artifacts inside /recipe to reuse them on every fresh replay.
Choose your own filenames, sources, build configuration, and transfer sequence.
Uploaded code and console access grant control over this DUT; they do not grant host shell access.
"""
