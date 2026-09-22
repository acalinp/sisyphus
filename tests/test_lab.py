import base64
import json
import os
import pty
import socket
from types import SimpleNamespace

import pytest

from sisyphus.capability import Capability
from sisyphus.errors import InfrastructureError
from sisyphus.lab import DeviceLease, DFUBoard
from sisyphus.observe import SerialLog


def rpc(capability, operation, **arguments):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(capability.directory / "socket"))
        connection.sendall(
            json.dumps({"operation": operation, "arguments": arguments}).encode() + b"\n"
        )
        return json.loads(connection.makefile("rb").readline())


def test_serial_capture_uses_fresh_offsets_and_retains_timestamped_bytes(tmp_path):
    master, slave = pty.openpty()
    try:
        with SerialLog(os.ttyname(slave), tmp_path) as serial:
            os.write(master, b"old U-Boot\n")
            assert serial.contains("old U-Boot", timeout=2)
            mark = serial.mark()
            assert not serial.contains("U-Boot", after=mark)
            os.write(master, b"new U-Boot\n")
            assert serial.contains("new U-Boot", after=mark, timeout=2)
            assert serial.tail() == "old U-Boot\nnew U-Boot\n"
            serial.write(b"fastboot usb 0\n")
            assert os.read(master, 100) == b"fastboot usb 0\n"
        assert (tmp_path / "serial.log").read_bytes() == b"old U-Boot\nnew U-Boot\n"
        events = [
            json.loads(line) for line in (tmp_path / "serial.events.jsonl").read_text().splitlines()
        ]
        assert events[0]["offset"] == 0
        assert sum(e["bytes"] for e in events) == len((tmp_path / "serial.log").read_bytes())
    finally:
        os.close(master)
        os.close(slave)


def test_serial_disconnect_is_infrastructure_failure(tmp_path):
    master, slave = pty.openpty()
    try:
        with pytest.raises(InfrastructureError, match="disconnected"):
            with SerialLog(os.ttyname(slave), tmp_path) as serial:
                os.close(master)
                master = None
                serial.contains("never", timeout=2)
    finally:
        if master is not None:
            os.close(master)
        os.close(slave)


def test_serial_storage_limit_is_enforced(tmp_path):
    master, slave = pty.openpty()
    try:
        with pytest.raises(InfrastructureError, match="byte budget"):
            with SerialLog(os.ttyname(slave), tmp_path, max_bytes=3) as serial:
                os.write(master, b"too much output")
                serial.contains("never", timeout=2)
        assert (tmp_path / "serial.log").stat().st_size <= 3
    finally:
        os.close(master)
        os.close(slave)


def test_device_lease_excludes_other_projects():
    with DeviceLease("fake-device-for-unit-test"):
        with pytest.raises(InfrastructureError, match="owns this device"):
            with DeviceLease("fake-device-for-unit-test"):
                pass
    with DeviceLease("fake-device-for-unit-test"):
        pass


def test_capability_errors_cannot_be_hidden_as_recipe_success():
    def handler(operation, arguments):
        raise InfrastructureError("lab failed")

    with pytest.raises(InfrastructureError, match="lab failed"):
        with Capability("board", handler) as capability:
            assert "error" in rpc(capability, "probe")
    assert not capability.active


def test_capability_rejects_invalid_envelopes_without_poisoning_lab():
    with Capability("board", lambda operation, args: {"ok": operation}) as capability:
        assert rpc(capability, "probe")["result"] == {"ok": "probe"}
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(capability.directory / "socket"))
            connection.sendall(b'{"command":"sh"}\n')
            assert "error" in json.loads(connection.makefile("rb").readline())
        capability.check()


@pytest.fixture
def board(tmp_path):
    device = DFUBoard(usb_port="1-2.3", serial_device="/dev/never-opened")
    device.directory = tmp_path
    device.serial = SimpleNamespace(check=lambda: None, write=lambda data: None)
    device.identity = lambda: device.dfu_id
    return device


def test_dfu_transport_pins_port_without_prescribing_payload_or_sequence(board):
    commands = []

    def command(args, timeout=15):
        commands.append(args)
        board.number += 1
        return (0, 'Found DFU: name="agent-chosen"\nDownload done.', False)

    board.command = command
    payload = base64.b64encode(b"fake firmware").decode()
    assert 'name="agent-chosen"' in board.dispatch("dfu_list", {})["text"]
    for alternate in ["agent-chosen", "another-stage", "agent-chosen", "0"]:
        response = board.dispatch("dfu_download", {"alternate": alternate, "data": payload})
        assert response["returncode"] == 0
        assert "transferred" not in response  # A process response cannot claim boot success.
    for command in commands:
        assert command[command.index("-p") + 1] == "1-2.3"
        assert "-d" not in command
        assert "-R" not in command
    record = next(board.directory.glob("payload-*.json"))
    assert json.loads(record.read_text())["bytes"] == 13
    board.dispatch("dfu_download", {"alternate": "agent-chosen", "data": payload, "reset": True})
    assert "-R" in commands[-1]
    with pytest.raises(ValueError):
        board.dispatch("shell", {"command": "rm -rf /"})


@pytest.mark.parametrize(
    "arguments",
    [
        {"alternate": "--device=other", "data": "eA=="},
        {"alternate": "bad\nvalue", "data": "eA=="},
        {"alternate": "0", "data": "eA==", "usb_port": "another"},
        {"alternate": "0", "data": "eA==", "reset": "yes"},
        {"alternate": "0", "data": "invalid-base64"},
    ],
)
def test_dfu_transport_rejects_invalid_arguments_before_host_command(board, arguments):
    board.command = lambda *a, **kw: pytest.fail("Must not invoke a host command")
    with pytest.raises(ValueError):
        board.dispatch("dfu_download", arguments)


def test_dfu_disappearance_is_reported_without_claiming_transfer_or_boot(board):
    board.command = lambda args, timeout=15: (74, "Download done.\nLIBUSB_ERROR_IO", False)
    result = board.dispatch("dfu_download", {"alternate": "agent-chosen", "data": "eA=="})
    assert result == {
        "returncode": 74,
        "text": "Download done.\nLIBUSB_ERROR_IO",
        "timed_out": False,
    }


def test_dfu_baseline_checks_rom_identity_and_actual_protocol(board):
    commands = []

    def command(args, timeout):
        commands.append(args)
        return 0, 'Found DFU: name="unknown-to-test"', False

    board.command = command
    board.reset_into_dfu(timeout=1)
    assert commands == [["dfu-util", "-p", "1-2.3", "-d", "0451:6165", "-l"]]


def test_fastboot_verification_is_a_scoped_host_query(board):
    commands = []

    def command(args, timeout):
        commands.append(args)
        return 0, "(bootloader) version-bootloader: U-Boot test\n", False

    board.command = command
    assert board.fastboot_version(timeout=1) == "U-Boot test"
    assert commands == [["fastboot", "-s", "usb:1-2.3", "getvar", "version-bootloader"]]
