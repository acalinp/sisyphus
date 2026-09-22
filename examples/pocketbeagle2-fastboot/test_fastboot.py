"""PocketBeagle 2: reset into ROM USB DFU, run a saved recipe, reach USB fastboot.

Set the two device paths below before use. Set reset_command to a trusted reset argv
for unattended iteration; otherwise reset manually into ROM DFU before each trial.
Run only with the board connected: SISYPHUS_HARDWARE=1 sisyphus solve test_fastboot.py
"""

import os

import pytest

from sisyphus.lab import DFUBoard


def test_fastboot(recipe, evidence):
    """Create a reusable `sh run` recipe that puts this PocketBeagle 2 in fastboot.

    Research and prepare any necessary artifacts once; save them in the recipe.
    Include README.md explaining usage, sources, and how to rebuild the artifacts.
    Serial and board-access documentation are available as on-demand evidence.
    """
    if os.environ.get("SISYPHUS_HARDWARE") != "1":
        pytest.skip("Connect/configure the board and set SISYPHUS_HARDWARE=1 to run")
    board = DFUBoard(
        usb_port="3-2.3",  # Physical port, e.g. 1-2.3
        serial_device="/dev/serial/by-id/usb-Raspberry_Pi_Debug_Probe__CMSIS-DAP__E664A836A32BAC31-if01",
        reset_command=["/home/calinp/p/fun/xiao-gpio/dist/gpioctl", "pulse"],  # Or ["/absolute/path/to/reset"]
    )
    with board.record(evidence):
        board.reset_into_dfu()
        result = recipe.run(["sh", "run"], access=[board.access])
        assert board.fastboot_version(timeout=30), "Board does not respond over USB fastboot"
        assert result.file("README.md").read_text().strip(), "Document usage and rebuilding"
