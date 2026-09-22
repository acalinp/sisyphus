from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from .config import Config, discover
from .errors import InfrastructureError
from .evidence import Attempt
from .recipe import Recipe
from .state import read_json

ATTEMPT = pytest.StashKey[Attempt]()
SETTINGS = pytest.StashKey[Config]()
TRIAL = pytest.StashKey[dict]()


def pytest_addoption(parser):
    parser.getgroup("sisyphus").addoption(
        "--sisyphus-config",
        type=Path,
        help="Path to trusted sisyphus.toml (otherwise discovered)",
    )
    parser.getgroup("sisyphus").addoption(
        "--sisyphus-trial",
        type=Path,
        help=argparse.SUPPRESS,
    )


def configuration_file(config, explicit):
    file = explicit or discover(config.rootpath)
    if file is None:
        file = discover(config.invocation_params.dir)
    if file is None:
        files = {
            found
            for selection in config.known_args_namespace.file_or_dir
            if (found := discover(config.invocation_params.dir / str(selection).split("::", 1)[0]))
        }
        if len(files) > 1:
            raise InfrastructureError("Select one Sisyphus project per pytest invocation")
        file = next(iter(files), None)
    return file


def untrusted(path: Path, settings: Config) -> bool:
    path = path.resolve()
    return any(
        path == root or root in path.parents
        for root in (settings.candidate, settings.state, *settings.references.values())
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    """Reject untrusted initial paths before pytest can import their conftest."""
    options = early_config.known_args_namespace
    file = None
    if options.sisyphus_trial:
        trial = read_json(options.sisyphus_trial)
        if trial.get("version") != 1:
            raise pytest.UsageError("Unsupported private trial specification")
        early_config.stash[TRIAL] = trial
        file = Path(trial["settings"]["file"])
    else:
        try:
            file = configuration_file(early_config, options.sisyphus_config)
        except (InfrastructureError, OSError) as exc:
            raise pytest.UsageError(str(exc)) from exc
    if file is None:
        return
    try:
        settings = (
            Config.restore(early_config.stash[TRIAL]["settings"])
            if TRIAL in early_config.stash
            else Config.load(file)
        )
    except InfrastructureError as exc:
        raise pytest.UsageError(str(exc)) from exc
    early_config.stash[SETTINGS] = settings
    if options.pyargs:
        raise pytest.UsageError(
            "Sisyphus requires explicit filesystem tests; --pyargs is unsupported"
        )
    selected = options.file_or_dir or early_config.getini("testpaths") or ["."]
    for selection in selected:
        path = (early_config.invocation_params.dir / str(selection).split("::", 1)[0]).resolve()
        # Pytest also probes immediate test* subdirectories for initial conftests.
        probes = [path, *(child for child in path.glob("test*") if child.is_dir())]
        if any(untrusted(probe, settings) for probe in probes):
            raise pytest.UsageError(
                "Refusing pytest discovery inside a recipe, reference, or evidence directory. "
                "Select the trusted test file explicitly."
            )


def pytest_configure(config):
    settings = config.stash.get(SETTINGS, None)
    if settings is None:
        return
    if getattr(config.option, "numprocesses", None):
        raise pytest.UsageError("Sisyphus does not yet support parallel pytest workers")
    try:
        config.stash[ATTEMPT] = Attempt(settings, config.stash.get(TRIAL, None))
    except OSError as exc:
        raise pytest.UsageError(str(exc)) from exc


def pytest_ignore_collect(collection_path, config):
    if SETTINGS in config.stash and untrusted(collection_path, config.stash[SETTINGS]):
        return True
    return None


def pytest_collection_finish(session):
    if ATTEMPT in session.config.stash:
        attempt = session.config.stash[ATTEMPT]
        attempt.collected = [item.nodeid for item in session.items]
        attempt.flush()


def pytest_deselected(items):
    if items and ATTEMPT in items[0].config.stash:
        attempt = items[0].config.stash[ATTEMPT]
        attempt.deselected.extend(item.nodeid for item in items)
        attempt.flush()


@pytest.fixture
def evidence(request) -> Attempt:
    """Trusted observation attachments and the current attempt directory."""
    if ATTEMPT not in request.config.stash:
        raise InfrastructureError("Create sisyphus.toml or pass --sisyphus-config")
    return request.config.stash[ATTEMPT]


@pytest.fixture
def recipe(request, evidence) -> Recipe:
    """The editable program; execution always uses a fresh rootless sandbox."""
    return Recipe(evidence, request.node.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if ATTEMPT not in item.config.stash:
        return
    attempt = item.config.stash[ATTEMPT]
    category = report.outcome
    if hasattr(report, "wasxfail"):
        category = "incomplete"
    elif report.failed:
        expected = call.excinfo and isinstance(
            call.excinfo.value,
            (AssertionError, pytest.fail.Exception),
        )
        category = "reject" if report.when == "call" and expected else "error"
    record = {"nodeid": report.nodeid, "phase": report.when, "outcome": category}
    if report.failed or report.skipped:
        directory = attempt.path / "tests"
        directory.mkdir(exist_ok=True)
        path = directory / f"{len(attempt.reports) + 1:04d}.txt"
        path.write_text(str(report.longrepr))
        record["evidence"] = str(path.relative_to(attempt.path))
        record["summary"] = str(report.longrepr)[-1500:]
    attempt.reports.append(record)
    attempt.flush()


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    report = (yield).get_result()
    if report.failed and ATTEMPT in collector.config.stash:
        attempt = collector.config.stash[ATTEMPT]
        directory = attempt.path / "tests"
        directory.mkdir(exist_ok=True)
        path = directory / f"collection-{len(attempt.reports):04d}.txt"
        path.write_text(str(report.longrepr))
        attempt.reports.append(
            {
                "nodeid": report.nodeid,
                "phase": "collection",
                "outcome": "error",
                "evidence": str(path.relative_to(attempt.path)),
                "summary": str(report.longrepr)[-1500:],
            }
        )
        attempt.flush()


def pytest_sessionfinish(session, exitstatus):
    if ATTEMPT not in session.config.stash:
        return
    attempt = session.config.stash[ATTEMPT]
    categories = {report["outcome"] for report in attempt.reports}
    if "error" in categories or exitstatus in (3, 4) or session.testsfailed and not categories:
        outcome = "error"
    elif exitstatus == 2:
        outcome = "error" if session.testsfailed else "incomplete"
    elif "reject" in categories:
        outcome = "reject"
    elif (
        exitstatus != 0
        or not attempt.reports
        or {"skipped", "incomplete"} & categories
        or bool(attempt.deselected)
        or not any(command["kind"] == "recipe" for command in attempt.commands)
        or any(command["status"] in {"running", "error"} for command in attempt.commands)
    ):
        outcome = "incomplete"
    else:
        outcome = "pass"
    attempt.outcome = outcome
    attempt.flush()
    # Ordinary pytest must not say success when Sisyphus acceptance is incomplete.
    if exitstatus == 0 and outcome != "pass":
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if ATTEMPT in terminalreporter.config.stash:
        attempt = terminalreporter.config.stash[ATTEMPT]
        terminalreporter.write_line(f"Sisyphus: {attempt.outcome}; evidence: {attempt.path}")
