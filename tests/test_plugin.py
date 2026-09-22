import json

import pytest

FAKE_BACKEND = """
import pytest
from sisyphus.errors import InfrastructureError
from sisyphus.sandbox import Completed

class Backend:
    def resolve(self, image):
        return "sha256:" + "a" * 64
    def session(self, image, work, references):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, args, env, directory, timeout):
        (directory / "stdout.log").write_text("hello, world\\n")
        (directory / "stderr.log").write_text("")
        if args[0] == "infra":
            raise InfrastructureError("engine unavailable")
        return Completed(1 if args[0] == "fail" else 0)

@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    monkeypatch.setattr("sisyphus.recipe.Podman", Backend)
"""


def setup_project(pytester, test, extra_conftest=""):
    (pytester.path / "recipe").mkdir()
    (pytester.path / "sisyphus.toml").write_text('[recipe]\npath = "recipe"\n')
    pytester.makeconftest(FAKE_BACKEND + extra_conftest)
    pytester.makepyfile(test_acceptance=test)


def verdict(pytester):
    files = list((pytester.path / ".sisyphus" / "attempts").glob("*/result.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text()), files[0].parent


@pytest.mark.parametrize(
    "body,expected",
    [
        ('assert recipe.run(["pass"]).stdout.strip() == "hello, world"', "pass"),
        ('recipe.run(["pass"]); assert False, "wrong greeting"', "reject"),
        ('recipe.run(["fail"])', "reject"),
        ('recipe.run(["infra"])', "error"),
        ('recipe.run(["pass"]); raise RuntimeError("fixture bug")', "error"),
        ('recipe.run(["pass"]); pytest.fail("not ready")', "reject"),
        ('recipe.run(["pass"]); pytest.skip("missing device")', "incomplete"),
        ('recipe.run(["pass"]); pytest.xfail("not implemented")', "incomplete"),
        ("assert True", "incomplete"),
    ],
)
def test_pytest_outcome_and_evidence(pytester, body, expected):
    setup_project(pytester, f"import pytest\ndef test_one(recipe):\n    {body}\n")
    result = pytester.runpytest_subprocess("-q")
    document, directory = verdict(pytester)
    assert document["outcome"] == expected
    assert (result.ret == 0) == (expected == "pass")
    assert any(report["phase"] == "teardown" for report in document["tests"])
    if expected in {"reject", "error"}:
        failure = next(report for report in document["tests"] if "evidence" in report)
        assert (directory / failure["evidence"]).is_file()


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_fixture_failures_are_infrastructure_errors(pytester, phase):
    fixture = "\n@pytest.fixture\ndef broken():\n"
    fixture += (
        '    assert False, "setup failed"\n'
        if phase == "setup"
        else ('    yield\n    assert False, "teardown failed"\n')
    )
    setup_project(pytester, 'def test_one(recipe, broken):\n    recipe.run(["pass"])', fixture)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret != 0
    assert verdict(pytester)[0]["outcome"] == "error"


def test_collection_error_is_not_repairable(pytester):
    setup_project(pytester, 'raise RuntimeError("cannot import trusted test")')
    result = pytester.runpytest_subprocess("-q")
    assert result.ret != 0
    assert verdict(pytester)[0]["outcome"] == "error"


def test_no_tests_is_incomplete(pytester):
    setup_project(pytester, "# no test functions")
    result = pytester.runpytest_subprocess("-q")
    assert result.ret != 0
    assert verdict(pytester)[0]["outcome"] == "incomplete"


def test_skip_alongside_pass_cannot_accept(pytester):
    setup_project(
        pytester,
        """
import pytest
def test_one(recipe):
    recipe.run(["pass"])
@pytest.mark.skip(reason="missing board")
def test_two():
    pass
""",
    )
    assert pytester.runpytest_subprocess("-q").ret != 0
    assert verdict(pytester)[0]["outcome"] == "incomplete"


def test_plugin_is_inert_without_configuration(pytester):
    pytester.makepyfile("def test_one(): assert True")
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)
    assert not (pytester.path / ".sisyphus").exists()


def test_invalid_configuration_is_usage_error(pytester):
    setup_project(pytester, "def test_one(recipe): pass")
    (pytester.path / "sisyphus.toml").write_text('[recipe]\npath="."\n')
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == 4


def test_does_not_collect_candidate_code_or_evidence(pytester):
    setup_project(pytester, 'def test_one(recipe):\n    recipe.run(["pass"])')
    for directory in (pytester.path / "recipe", pytester.path / ".sisyphus"):
        directory.mkdir(exist_ok=True)
        (directory / "conftest.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
        (directory / "test_bad.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)
    assert verdict(pytester)[0]["outcome"] == "pass"


def test_explicit_candidate_test_rejected_before_conftest_import(pytester):
    setup_project(pytester, "def test_one(recipe): pass")
    (pytester.path / "recipe" / "conftest.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
    (pytester.path / "recipe" / "test_bad.py").write_text("def test_bad(): pass")
    result = pytester.runpytest_subprocess("recipe/test_bad.py", "-q")
    assert result.ret == 4
    assert "UNTRUSTED IMPORT" not in result.stdout.str() + result.stderr.str()


def test_implicit_test_prefix_candidate_is_rejected_before_import(pytester):
    setup_project(pytester, "def test_one(recipe): pass")
    (pytester.path / "recipe").rename(pytester.path / "test_candidate")
    (pytester.path / "sisyphus.toml").write_text('[recipe]\npath="test_candidate"\n')
    (pytester.path / "test_candidate" / "conftest.py").write_text(
        'raise RuntimeError("UNTRUSTED IMPORT")'
    )
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == 4
    assert "UNTRUSTED IMPORT" not in result.stdout.str() + result.stderr.str()


def test_toml_works_from_outside_project_and_excludes_candidate(pytester):
    pytester.makeconftest(FAKE_BACKEND)
    experiment = pytester.path / "experiment"
    experiment.mkdir()
    file = experiment / "test_goal.py"
    file.write_text('def test_one(recipe): recipe.run(["pass"])')
    (experiment / "sisyphus.toml").write_text("[recipe]")
    recipe = experiment / "recipe"
    recipe.mkdir()
    (recipe / "conftest.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
    (recipe / "test_bad.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
    result = pytester.runpytest_subprocess(str(file), "-q")
    result.assert_outcomes(passed=1)
    saved = list((experiment / ".sisyphus/attempts").glob("*/result.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["outcome"] == "pass"
    result = pytester.runpytest_subprocess(str(recipe / "test_bad.py"), "-q")
    assert result.ret == 4
    assert "UNTRUSTED IMPORT" not in result.stdout.str() + result.stderr.str()


def test_toml_empty_recipe_runs_from_initial_failure(pytester):
    pytester.makeconftest(FAKE_BACKEND)
    pytester.makepyfile(test_goal='def test_one(recipe): recipe.run(["fail"])')
    (pytester.path / "sisyphus.toml").write_text("[recipe]")
    result = pytester.runpytest_subprocess("test_goal.py", "-q")
    result.assert_outcomes(failed=1)
    assert (pytester.path / "recipe").is_dir()
    assert verdict(pytester)[0]["outcome"] == "reject"


@pytest.mark.parametrize(
    "nested_configuration",
    [
        ("test_bad.py", 'SISYPHUS = {}\nraise RuntimeError("UNTRUSTED IMPORT")'),
        ("sisyphus.toml", "[recipe]"),
    ],
)
def test_candidate_cannot_redeclare_trust_under_toml_project(pytester, nested_configuration):
    pytester.makepyfile(test_goal="def test_one(recipe): pass")
    (pytester.path / "sisyphus.toml").write_text("[recipe]")
    candidate = pytester.path / "recipe"
    candidate.mkdir()
    (candidate / "recipe").mkdir()
    (candidate / "conftest.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
    (candidate / "test_bad.py").write_text('raise RuntimeError("UNTRUSTED IMPORT")')
    name, content = nested_configuration
    (candidate / name).write_text(content)
    result = pytester.runpytest_subprocess("recipe/test_bad.py", "-q")
    assert result.ret == 4
    assert "UNTRUSTED IMPORT" not in result.stdout.str() + result.stderr.str()


def test_inline_settings_do_not_configure_pytest(pytester):
    pytester.makepyfile(test_goal='SISYPHUS = {}\ndef test_one(recipe): pass')
    result = pytester.runpytest_subprocess("test_goal.py", "-q")
    result.assert_outcomes(errors=1)
    assert "Create sisyphus.toml" in result.stdout.str()
    assert not (pytester.path / "recipe").exists()
    assert not (pytester.path / ".sisyphus").exists()
