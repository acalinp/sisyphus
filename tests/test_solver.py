import json
import os
from pathlib import Path

import pytest

from sisyphus import InfrastructureError
from sisyphus.config import Config
from sisyphus.evidence import write_json
from sisyphus.feedback import feedback
from sisyphus.snapshot import snapshot
from sisyphus.solver import Solver, pytest_environment
from sisyphus.state import ProjectLock, Run


class Backend:
    def resolve(self, image):
        return "sha256:" + "a" * 64

    def cleanup_trials(self, run_id):
        pass


@pytest.fixture
def solve_project(project, tmp_path):
    config = project()
    (tmp_path / "editor").mkdir()
    (tmp_path / "editor" / "repair").write_text("# test adapter")
    with config.file.open("a") as handle:
        handle.write('[editor]\npath="editor"\ncommand=["sh", "/editor/repair"]\n')
    (tmp_path / "test_goal.py").write_text("def test_goal(recipe): pass")
    (config.candidate / "value").write_text("wrong")
    return Config.load(config.file)


class Driver:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, run, entry):
        self.calls += 1
        next_outcome = self.outcomes.pop(0)
        if isinstance(next_outcome, BaseException):
            raise next_outcome
        directory = run.path / "attempts" / f"{entry['number']:04d}"
        digest = snapshot(run.config.candidate, directory / "source")
        (directory / "evidence").mkdir()
        document = {
            "version": 1,
            "outcome": next_outcome,
            "image": run.data["image"],
            "digests": {"recipe": digest},
            "tests": [],
            "commands": [],
        }
        write_json(directory / "evidence" / "result.json", document)
        return document


class Editor:
    def __init__(self, run, *, status="changed", fail=None):
        self.run = run
        self.status = status
        self.fail = fail
        self.present = False
        self.calls = 0
        self.frozen = False

    def exists(self):
        return self.present

    def ensure(self):
        self.present = True
        self.frozen = True
        self.run.data["workshop_created"] = True

    def freeze(self):
        self.frozen = True

    def remove(self):
        self.present = False
        self.run.data["workshop_created"] = False

    def repair(self, file, directory):
        self.calls += 1
        document = json.loads(file.read_text())
        assert document["outcome"] == "reject"
        self.frozen = True
        if self.fail:
            raise self.fail
        if self.status == "changed":
            (self.run.config.candidate / "value").write_text(f"fixed {self.calls}")
        return {"version": 1, "status": self.status}


def make_run(config, limit=3):
    return Run.create(config, str(config.file.parent / "test_goal.py"), limit, 30, Backend())


def execute(run, driver, editor):
    with ProjectLock(run.config.state) as lock:
        return Solver(
            run,
            lock,
            backend=Backend(),
            driver=driver,
            workshop=editor,
            report=lambda message: None,
        ).execute()


def test_repairs_then_exports_exact_successful_snapshot(solve_project):
    run = make_run(solve_project)
    editor = Editor(run)
    execute(run, Driver(["reject", "pass"]), editor)
    assert run.data["phase"] == "accepted"
    assert len(run.data["attempts"]) == 2
    assert len(run.data["repairs"]) == 1
    assert not editor.present
    accepted = run.path / "accepted" / "recipe" / "value"
    assert accepted.read_text() == "fixed 1"
    (solve_project.candidate / "value").write_text("later edit")
    assert accepted.read_text() == "fixed 1"
    assert Run.load(run.path).data["phase"] == "accepted"
    execute(Run.load(run.path), Driver([]), editor)


@pytest.mark.parametrize("outcome", ["error", "incomplete"])
def test_no_repair_for_infrastructure_or_incomplete_tests(solve_project, outcome):
    run = make_run(solve_project)
    editor = Editor(run)
    execute(run, Driver([outcome]), editor)
    assert run.data["phase"] == outcome
    assert not run.data["repairs"]
    assert not editor.calls


def test_attempt_budget_has_no_unused_final_repair(solve_project):
    run = make_run(solve_project, limit=2)
    editor = Editor(run)
    execute(run, Driver(["reject", "reject"]), editor)
    assert run.data["phase"] == "exhausted"
    assert len(run.data["attempts"]) == 2
    assert editor.calls == 1
    assert editor.frozen


def test_resume_preserves_spent_budget_and_records_interrupted_attempt(solve_project):
    run = make_run(solve_project)
    editor = Editor(run)
    with pytest.raises(KeyboardInterrupt):
        execute(run, Driver([KeyboardInterrupt()]), editor)
    loaded = Run.load(run.path)
    assert loaded.data["phase"] == "interrupted"
    assert len(loaded.data["attempts"]) == 1
    execute(loaded, Driver(["pass"]), Editor(loaded))
    assert loaded.data["phase"] == "accepted"
    assert [attempt["status"] for attempt in loaded.data["attempts"]] == ["interrupted", "pass"]


def test_resume_after_interrupted_edit_tests_before_any_new_repair(solve_project):
    run = make_run(solve_project)
    editor = Editor(run, fail=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        execute(run, Driver(["reject"]), editor)
    loaded = Run.load(run.path)
    next_editor = Editor(loaded)
    execute(loaded, Driver(["pass"]), next_editor)
    assert loaded.data["repairs"][0]["status"] == "interrupted"
    assert next_editor.calls == 0


def test_saved_references_do_not_follow_live_reference_edits(project, tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "input").write_text("original")
    config = project(references={"base": ref})
    (tmp_path / "editor").mkdir()
    (tmp_path / "editor" / "repair").write_text("# adapter")
    with config.file.open("a") as handle:
        handle.write('[editor]\npath="editor"\ncommand=["sh", "/editor/repair"]\n')
    (tmp_path / "test_goal.py").write_text("def test_goal(recipe): pass")
    run = make_run(Config.load(config.file))
    (ref / "input").write_text("changed")
    Run.load(run.path).verify_trusted()
    assert (Path(run.data["references"]["base"]) / "input").read_text() == "original"


def test_resume_rejects_changed_test_before_spending_an_attempt(solve_project):
    run = make_run(solve_project)
    (solve_project.file.parent / "test_goal.py").write_text("def test_goal(): assert True")
    execute(run, Driver([]), Editor(run))
    assert run.data["phase"] == "error"
    assert "Trusted project files changed" in run.data["last_error"]
    assert not run.data["attempts"]


def test_project_lock_excludes_another_solver(solve_project):
    with ProjectLock(solve_project.state):
        with pytest.raises(InfrastructureError, match="lock"):
            with ProjectLock(solve_project.state):
                pass
    with ProjectLock(solve_project.state):
        pass


def test_blocked_editor_stops_and_remains_frozen(solve_project):
    run = make_run(solve_project)
    editor = Editor(run, status="blocked")
    execute(run, Driver(["reject"]), editor)
    assert run.data["phase"] == "blocked"
    assert editor.frozen
    assert len(run.data["attempts"]) == 1


def test_child_pytest_cannot_inherit_selection_or_assertion_overrides(monkeypatch):
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PYTHONOPTIMIZE"):
        monkeypatch.setenv(name, "unwanted")
    environment = pytest_environment()
    assert not set(environment) & {
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTHONPATH",
        "PYTHONOPTIMIZE",
    }
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PATH"] == os.environ["PATH"]


def test_feedback_does_not_embed_large_logs(solve_project):
    run = make_run(solve_project)
    directory = run.path / "attempts" / "0001" / "evidence"
    directory.mkdir(parents=True)
    (directory / "large.log").write_text("private log body" * 100000)
    run.data["attempts"].append({"number": 1})
    result = {
        "outcome": "reject",
        "digests": {"recipe": "a" * 64},
        "tests": [],
        "commands": [{"kind": "recipe", "stdout": "large.log", "stderr": "absent.log"}],
    }
    document = feedback(run, {"number": 1}, result)
    assert "private log body" not in json.dumps(document)
    assert len(json.dumps(document)) < 4096
    assert document["evidence"][0]["bytes"] == (directory / "large.log").stat().st_size


def test_toml_run_retains_settings_and_exports_prepared_artifacts(tmp_path):
    test = tmp_path / "test_goal.py"
    test.write_text("def test_goal(recipe): pass")
    file = tmp_path / "sisyphus.toml"
    file.write_text('[editor]\nmodel = "local"\nbase_url = "http://localhost/v1"')
    config = Config.load(file)
    artifact = config.candidate / "prepared.bin"
    artifact.write_bytes(b"prepared once")
    run = make_run(config)
    loaded = Run.load(run.path)
    loaded.verify_trusted()
    assert loaded.config == config
    execute(loaded, Driver(["pass"]), Editor(loaded))
    assert loaded.data["phase"] == "accepted"
    assert (loaded.path / "accepted/recipe/prepared.bin").read_bytes() == b"prepared once"
    test.write_text(test.read_text() + "\n# modified contract")
    with pytest.raises(InfrastructureError, match="Trusted project files changed"):
        loaded.verify_trusted()
