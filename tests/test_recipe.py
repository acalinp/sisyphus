import json

import pytest

from sisyphus import InfrastructureError, Recipe, RecipeRejected
from sisyphus.evidence import Attempt
from sisyphus.sandbox import Completed


class FakeBackend:
    """An injected test double; never executes recipe code on the host."""

    def __init__(self, results=None):
        self.results = list(results or [Completed(0)])
        self.workspaces = []
        self.commands = []
        self.closed = 0

    def resolve(self, image):
        return "sha256:" + "a" * 64

    def session(self, image, work, references):
        self.workspaces.append(work)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed += 1

    def execute(self, args, env, directory, timeout):
        self.commands.append(args)
        (directory / "stdout.log").write_text("hello\n")
        (directory / "stderr.log").write_text("")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_each_call_has_fresh_files_and_frozen_source(project):
    config = project()
    (config.candidate / "source").write_text("first")
    backend = FakeBackend([Completed(0), Completed(0)])
    recipe = Recipe(Attempt(config), "test_one", backend)
    first = recipe.run(["build"])
    (first.directory / "generated").write_text("local")
    (config.candidate / "source").write_text("later edit")
    second = recipe.run(["build"])
    assert first.stdout == "hello\n"
    assert not (second.directory / "generated").exists()
    assert second.file("source").read_text() == "first"
    assert backend.closed == 2


def test_setup_failure_cannot_be_suppressed(project):
    backend = FakeBackend([Completed(2)])
    recipe = Recipe(Attempt(project(setup=["sh", "setup"])), "test", backend)
    with pytest.raises(RecipeRejected, match="setup command exited 2"):
        recipe.run(["build"], check=False)
    assert backend.commands == [("sh", "setup")]
    assert backend.closed == 1


def test_expected_nonzero_command(project):
    backend = FakeBackend([Completed(42)])
    recipe = Recipe(Attempt(project()), "test", backend)
    assert recipe.run(["expected-failure"], check=False).returncode == 42


@pytest.mark.parametrize(
    "result,exception",
    [
        (Completed(1), RecipeRejected),
        (Completed(-9, timed_out=True), RecipeRejected),
        (Completed(-9, output_limited=True), RecipeRejected),
        (InfrastructureError("engine broke"), InfrastructureError),
    ],
)
def test_failure_retains_evidence_and_cleans_up(project, result, exception):
    backend = FakeBackend([result])
    attempt = Attempt(project())
    with pytest.raises(exception):
        Recipe(attempt, "test", backend).run(["boot"])
    assert backend.closed == 1
    document = json.loads((attempt.path / "result.json").read_text())
    command = document["commands"][0]
    assert command["status"] == ("error" if exception is InfrastructureError else "failed")
    assert (attempt.path / command["stdout"]).read_text() == "hello\n"


@pytest.mark.parametrize("relative", ["/etc/passwd", "../result.json", "link", "linkdir/file"])
def test_output_access_rejects_escape(project, relative):
    result = Recipe(Attempt(project()), "test", FakeBackend()).run(["build"])
    (result.directory / "link").symlink_to("/etc/passwd")
    (result.directory / "linkdir").symlink_to("/etc")
    with pytest.raises((InfrastructureError, RecipeRejected)):
        result.file(relative)


def test_missing_output_is_candidate_rejection(project):
    result = Recipe(Attempt(project()), "test", FakeBackend()).run(["build"])
    with pytest.raises(RecipeRejected, match="missing"):
        result.file("absent")


def test_reference_snapshot_and_attachment(project, tmp_path):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "input").write_text("original")
    attempt = Attempt(project(references={"base": reference}))
    Recipe(attempt, "test", FakeBackend()).run(["build"])
    (reference / "input").write_text("changed")
    assert (attempt.references["base"] / "input").read_text() == "original"
    log = tmp_path / "serial.log"
    log.write_text("booting")
    attached = attempt.attach(log, name="serial.log", description="Serial boot output")
    assert attached.read_text() == "booting"
    with pytest.raises(InfrastructureError):
        attempt.attach(log, name="../escape")


def test_rejects_reserved_environment_before_execution(project):
    backend = FakeBackend()
    with pytest.raises(InfrastructureError, match="reserved"):
        Recipe(Attempt(project()), "test", backend).run(["boot"], env={"SISYPHUS_TOKEN": "x"})
    assert not backend.workspaces
