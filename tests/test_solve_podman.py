import json
import os
import subprocess
import sys
import time

import pytest

from sisyphus.editor import Workshop
from sisyphus.errors import InfrastructureError
from sisyphus.sandbox import Podman
from sisyphus.state import ProjectLock, Run

pytestmark = pytest.mark.podman


@pytest.fixture
def solve_lab(tmp_path):
    image = os.environ.get("SISYPHUS_TEST_IMAGE")
    if not image:
        pytest.skip("Set SISYPHUS_TEST_IMAGE to an existing local image")
    (tmp_path / "recipe").mkdir()
    (tmp_path / "editor").mkdir()
    (tmp_path / "sisyphus.toml").write_text(
        f"[recipe]\nimage={json.dumps(image)}\ntimeout=15\n"
        '[editor]\npath="editor"\ncommand=["sh", "/editor/repair"]\ntimeout=15\n'
    )
    (tmp_path / "recipe" / "hello").write_text("#!/bin/sh\necho wrong\n")
    (tmp_path / "test_goal.py").write_text(
        "def test_goal(recipe):\n"
        '    result = recipe.run(["sh", "hello"])\n'
        '    assert result.stdout.strip() == "correct"\n'
    )
    (tmp_path / "editor" / "repair").write_text(
        "set -eu\ncat > /tmp/feedback.json\n"
        "printf '#!/bin/sh\\necho correct\\n' > /recipe/hello\n"
        'printf \'%s\\n\' \'{"version":1,"status":"changed"}\'\n'
    )
    yield tmp_path
    # Clean up only containers belonging to these explicitly created test runs.
    for directory in (tmp_path / ".sisyphus" / "runs").glob("*"):
        if (directory / "run.json").exists():
            run = Run.load(directory)
            backend = Podman()
            Workshop(run, backend).remove()
            backend.cleanup_trials(run.data["id"])


def cli(project, *args, env=None):
    result = subprocess.run(
        [sys.executable, "-m", "sisyphus.cli", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return result


def latest_run(project):
    directories = list((project / ".sisyphus" / "runs").iterdir())
    assert len(directories) == 1
    return Run.load(directories[0])


def test_real_cli_repairs_and_exports(solve_lab):
    result = cli(solve_lab, "solve", "test_goal.py", "--attempts", "3")
    assert result.returncode == 0, result.stdout + result.stderr
    run = latest_run(solve_lab)
    assert run.data["phase"] == "accepted"
    assert [entry["status"] for entry in run.data["attempts"]] == ["reject", "pass"]
    assert len(run.data["repairs"]) == 1
    assert len(run.data["collected"]) == 1
    assert (run.path / "accepted" / "recipe" / "hello").read_text().endswith("echo correct\n")
    response = cli(solve_lab, "solve", "--resume", str(run.path))
    assert response.returncode == 0, response.stdout + response.stderr
    assert len(latest_run(solve_lab).data["attempts"]) == 2


def test_real_editor_root_persists_but_is_absent_from_trials(solve_lab):
    (solve_lab / "editor" / "repair").write_text(
        "set -eu\ncat >/tmp/feedback.json\n"
        "if [ -e /opt/workshop-marker ]; then word=correct; else word=still-wrong; fi\n"
        "touch /opt/workshop-marker\n"
        "printf '#!/bin/sh\\ntest ! -e /opt/workshop-marker || exit 9\\necho %s\\n' "
        '"$word" > /recipe/hello\n'
        'printf \'%s\\n\' \'{"version":1,"status":"changed"}\'\n'
    )
    result = cli(solve_lab, "solve", "test_goal.py", "--attempts", "3")
    assert result.returncode == 0, result.stdout + result.stderr
    run = latest_run(solve_lab)
    assert len(run.data["attempts"]) == 3
    assert len(run.data["repairs"]) == 2


def test_real_resume_increases_budget_without_resetting_it(solve_lab):
    result = cli(solve_lab, "solve", "test_goal.py", "--attempts", "1")
    assert result.returncode == 1, result.stdout + result.stderr
    run = latest_run(solve_lab)
    assert run.data["phase"] == "exhausted"
    result = cli(solve_lab, "solve", "--resume", str(run.path), "--attempts", "3")
    assert result.returncode == 0, result.stdout + result.stderr
    resumed = latest_run(solve_lab)
    assert len(resumed.data["attempts"]) == 3
    assert len(resumed.data["repairs"]) == 1


def test_real_editor_cannot_write_tests_or_evidence(solve_lab):
    (solve_lab / "editor" / "repair").write_text(
        "set -eu\ncat >/tmp/feedback.json\n"
        "if echo changed > /tests/test_goal.py; then exit 5; fi\n"
        "if echo changed > /evidence/0001/evidence/result.json; then exit 6; fi\n"
        "test ! -e /run/podman/podman.sock\n"
        "printf '#!/bin/sh\\necho correct\\n' > /recipe/hello\n"
        'printf \'%s\\n\' \'{"version":1,"status":"changed"}\'\n'
    )
    result = cli(solve_lab, "solve", "test_goal.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert '== "correct"' in (solve_lab / "test_goal.py").read_text()
    run = latest_run(solve_lab)
    feedback = json.loads((run.path / "repairs" / "0001" / "feedback.json").read_text())
    assert feedback["evidence"]
    assert len(json.dumps(feedback)) < 16000


@pytest.mark.parametrize("failure", ["skip", "exception"])
def test_real_infrastructure_or_skip_never_invokes_editor(solve_lab, failure):
    body = 'pytest.skip("no board")' if failure == "skip" else 'raise RuntimeError("broken lab")'
    (solve_lab / "test_goal.py").write_text(f"import pytest\ndef test_goal(recipe):\n    {body}\n")
    result = cli(solve_lab, "solve", "test_goal.py")
    assert result.returncode != 0
    run = latest_run(solve_lab)
    assert run.data["phase"] == ("incomplete" if failure == "skip" else "error")
    assert not run.data["repairs"]
    assert not run.data["workshop_created"]


def test_real_worker_ignores_ambient_pytest_selection(solve_lab):
    environment = dict(os.environ, PYTEST_ADDOPTS="-k nonexistent", PYTEST_PLUGINS="missing_plugin")
    result = cli(solve_lab, "solve", "test_goal.py", env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert latest_run(solve_lab).data["collected"] == ["test_goal.py::test_goal"]


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for test synchronization")


def start_solver(project):
    return subprocess.Popen(
        [sys.executable, "-m", "sisyphus.cli", "solve", "test_goal.py", "--attempts", "4"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_real_resume_after_parent_dies_during_repair(solve_lab):
    (solve_lab / "editor" / "repair").write_text(
        "set -eu\ncat >/tmp/feedback.json\n"
        "if [ ! -e /opt/persisted ]; then\n"
        "    touch /opt/persisted /recipe/repair-started\n"
        "    sleep 60\n"
        "fi\n"
        "printf '#!/bin/sh\\necho correct\\n' > /recipe/hello\n"
        'printf \'%s\\n\' \'{"version":1,"status":"changed"}\'\n'
    )
    process = start_solver(solve_lab)
    try:
        wait_until(lambda: (solve_lab / "recipe" / "repair-started").exists())
        process.kill()
        process.wait(timeout=5)
        run = latest_run(solve_lab)
        assert run.data["phase"] == "repairing"
        result = cli(solve_lab, "solve", "--resume", str(run.path))
        assert result.returncode == 0, result.stdout + result.stderr
        resumed = latest_run(solve_lab)
        assert len(resumed.data["attempts"]) == 3
        assert [repair["status"] for repair in resumed.data["repairs"]] == [
            "interrupted",
            "changed",
        ]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_parent_death_stops_test_and_preserves_spent_budget(solve_lab):
    (solve_lab / "recipe" / "hello").write_text("#!/bin/sh\ntouch started\nsleep 60\necho wrong\n")
    process = start_solver(solve_lab)
    try:
        wait_until(
            lambda: bool(
                list(
                    (solve_lab / ".sisyphus" / "runs").glob(
                        "*/attempts/0001/evidence/work/1/started"
                    )
                )
            )
        )
        process.kill()
        process.wait(timeout=5)
        run = latest_run(solve_lab)

        def lock_released():
            try:
                with ProjectLock(run.config.state):
                    return True
            except InfrastructureError:
                return False

        wait_until(lock_released, timeout=10)
        (solve_lab / "recipe" / "hello").write_text("#!/bin/sh\necho correct\n")
        result = cli(solve_lab, "solve", "--resume", str(run.path))
        assert result.returncode == 0, result.stdout + result.stderr
        resumed = latest_run(solve_lab)
        assert [attempt["status"] for attempt in resumed.data["attempts"]] == [
            "interrupted",
            "pass",
        ]
        assert not resumed.data["repairs"]
        containers = Podman().control(
            "ps",
            "-a",
            "--filter",
            f"label=io.sisyphus.run={run.data['id']}",
            "--format",
            "{{.ID}}",
        )
        assert containers == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_whole_test_timeout_is_infrastructure_failure(solve_lab):
    (solve_lab / "test_goal.py").write_text(
        "import time\ndef test_goal(recipe):\n    time.sleep(60)\n"
    )
    result = cli(solve_lab, "solve", "test_goal.py", "--test-timeout", "1")
    assert result.returncode == 2, result.stdout + result.stderr
    run = latest_run(solve_lab)
    assert run.data["phase"] == "error"
    assert "time/output budget" in run.data["last_error"]
    assert not run.data["repairs"]


def test_real_discard_releases_blocked_workshop(solve_lab):
    (solve_lab / "editor" / "repair").write_text(
        "cat >/tmp/feedback.json\n"
        'printf \'%s\\n\' \'{"version":1,"status":"blocked","message":"Need input"}\'\n'
    )
    result = cli(solve_lab, "solve", "test_goal.py")
    assert result.returncode == 1
    run = latest_run(solve_lab)
    assert run.data["phase"] == "blocked"
    assert Workshop(run, Podman()).exists()
    result = cli(solve_lab, "discard", str(run.path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert latest_run(solve_lab).data["phase"] == "discarded"
    assert not Workshop(run, Podman()).exists()


def test_real_candidate_cannot_narrow_collected_tests(solve_lab):
    (solve_lab / "recipe" / "two-tests").write_text("present")
    (solve_lab / "test_goal.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        '@pytest.mark.parametrize("case", [1, 2] if Path("recipe/two-tests").exists() else [1])\n'
        "def test_goal(recipe, case):\n"
        '    assert recipe.run(["sh", "hello"]).stdout.strip() == "correct"\n'
    )
    script = solve_lab / "editor" / "repair"
    script.write_text(script.read_text() + "rm -f /recipe/two-tests\n")
    result = cli(solve_lab, "solve", "test_goal.py")
    assert result.returncode == 2, result.stdout + result.stderr
    run = latest_run(solve_lab)
    assert run.data["phase"] == "error"
    assert "Collected tests changed" in run.data["last_error"]
    assert not (run.path / "accepted").exists()
