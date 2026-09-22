import os
import subprocess

import pytest

from sisyphus import Recipe, RecipeRejected
from sisyphus.evidence import Attempt

pytestmark = pytest.mark.podman


@pytest.fixture
def image():
    value = os.environ.get("SISYPHUS_TEST_IMAGE")
    if not value:
        pytest.skip("Set SISYPHUS_TEST_IMAGE to an existing local image with sh and sleep infinity")
    # Explicit opt-in means unavailable/broken Podman must fail, not silently skip.
    return value


def test_real_execution_is_fresh_and_offline(project, image):
    config = project(image=image)
    attempt = Attempt(config)
    recipe = Recipe(attempt, "real_fresh")
    first = recipe.run(["sh", "-c", "echo hello; touch /tmp/workshop-only; echo built > output"])
    assert first.stdout.strip() == "hello"
    assert first.file("output").read_text().strip() == "built"
    second = recipe.run(
        [
            "sh",
            "-c",
            "set -e; test ! -e /tmp/workshop-only; test ! -e output; ls /sys/class/net",
        ]
    )
    assert second.stdout.strip() == "lo"
    assert attempt.image and len(attempt.image.removeprefix("sha256:")) == 64


def test_real_setup_and_readonly_reference(project, tmp_path, image):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "data").write_text("original")
    config = project(
        image=image,
        references={"input": reference},
        setup=["sh", "-c", "echo installed > /opt/sisyphus-test-dependency"],
    )
    recipe = Recipe(Attempt(config), "real_setup")
    result = recipe.run(
        [
            "sh",
            "-c",
            "cat /opt/sisyphus-test-dependency; "
            "if echo changed > /refs/input/data; then exit 1; fi; cat /refs/input/data",
        ]
    )
    assert result.stdout.splitlines() == ["installed", "original"]
    assert (reference / "data").read_text() == "original"


def test_real_timeout_removes_container(project, image):
    recipe = Recipe(Attempt(project(image=image)), "real_timeout")
    with pytest.raises(RecipeRejected, match="timed out|budget"):
        recipe.run(["sleep", "30"], timeout=2)
    # Record the engine's complete list only to assert this suite leaked no own containers.
    names = subprocess.check_output(["podman", "ps", "-a", "--format", "{{.Names}}"], text=True)
    assert not any(name.startswith("sisyphus-") for name in names.splitlines())


def test_real_failed_command_and_no_host_environment(project, image, monkeypatch):
    monkeypatch.setenv("SISYPHUS_HOST_SECRET", "must-not-enter-replay")
    monkeypatch.setenv("HTTP_PROXY", "http://secret@proxy.invalid:9999")
    recipe = Recipe(Attempt(project(image=image)), "real_failure")
    result = recipe.run(
        ["sh", "-c", 'test -z "$SISYPHUS_HOST_SECRET$HTTP_PROXY" || exit 17; exit 42'],
        check=False,
    )
    assert result.returncode == 42
    with pytest.raises(RecipeRejected, match="exited 9"):
        recipe.run(["sh", "-c", "exit 9"])
