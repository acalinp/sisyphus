import os

import pytest

from sisyphus.config import Config, argv, discover, duration
from sisyphus.errors import InfrastructureError
from sisyphus.snapshot import snapshot


def test_paths_are_relative_to_config(project, monkeypatch, tmp_path):
    config = project()
    monkeypatch.chdir("/tmp")
    loaded = Config.load(config.file)
    assert loaded.candidate == tmp_path / "recipe"
    assert discover(loaded.candidate) == config.file


@pytest.mark.parametrize(
    "extra",
    [
        "\nunknown = true",
        '\nnetwork = "internet"',
        '\nsetup = "sh build"',
        "\ntimeout = -1",
        "\ntimeout = true",
        '\nimage = "--privileged"',
    ],
)
def test_rejects_invalid_recipe_settings(project, extra):
    config = project()
    with config.file.open("a") as handle:
        handle.write(extra)
    with pytest.raises(InfrastructureError):
        Config.load(config.file)


def test_rejects_unknown_top_level(project):
    config = project()
    config.file.write_text('unknown = "yes"\n' + config.file.read_text())
    with pytest.raises(InfrastructureError, match="Unknown configuration"):
        Config.load(config.file)


def test_rejects_overlapping_reference(project, tmp_path):
    with pytest.raises(InfrastructureError, match="overlap"):
        project(references={"bad": tmp_path})


def test_rejects_candidate_containing_test_project(project):
    config = project()
    config.file.write_text('[recipe]\npath = "."\n')
    with pytest.raises(InfrastructureError, match="trusted project"):
        Config.load(config.file)


def test_rejects_state_symlink(project, tmp_path):
    config = project()
    (tmp_path / ".sisyphus").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InfrastructureError, match="symbolic link"):
        Config.load(config.file)


@pytest.mark.parametrize("value", ["sh build", [], [1], ["bad\x00"], None])
def test_argument_validation(value):
    with pytest.raises(InfrastructureError):
        argv(value, "command")


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "30s"])
def test_duration_validation(value):
    with pytest.raises(InfrastructureError):
        duration(value)


def test_snapshot_digest_tracks_bytes_names_and_executable_bit(project, tmp_path):
    config = project()
    script = config.candidate / "boot"
    script.write_text("one")
    first = snapshot(config.candidate, tmp_path / "a")
    assert first == snapshot(config.candidate, tmp_path / "b")
    script.write_text("two")
    changed = snapshot(config.candidate, tmp_path / "c")
    assert changed != first
    script.chmod(0o755)
    assert changed != snapshot(config.candidate, tmp_path / "d")
    assert (tmp_path / "a" / "boot").read_text() == "one"


@pytest.mark.parametrize("special", ["symlink", "fifo"])
def test_snapshot_rejects_special_files(project, tmp_path, special):
    config = project()
    path = config.candidate / "bad"
    if special == "symlink":
        path.symlink_to("/etc/passwd")
    else:
        os.mkfifo(path)
    with pytest.raises(InfrastructureError, match="unsupported"):
        snapshot(config.candidate, tmp_path / "copy")


@pytest.mark.parametrize(
    "editor",
    [
        'path="recipe"\ncommand=["sh", "repair"]',
        'path="missing"\ncommand=["sh", "repair"]',
        'path="editor"\ncommand="sh repair"',
        'path="editor"\ncommand=["sh", "repair"]\nnetwork=true',
    ],
)
def test_editor_configuration_is_strict_and_separate(project, tmp_path, editor):
    config = project()
    (tmp_path / "editor").mkdir()
    with config.file.open("a") as handle:
        handle.write(f"[editor]\n{editor}\n")
    with pytest.raises(InfrastructureError):
        Config.load(config.file)


def test_toml_starts_empty_and_preserves_prepared_artifacts(tmp_path):
    file = tmp_path / "sisyphus.toml"
    file.write_text("[recipe]\ntimeout = 17\n")
    assert discover(file) == file
    config = Config.load(file)
    assert config.timeout == 17
    assert list(config.candidate.iterdir()) == []
    assert Config.restore(config.record()) == config
    (config.candidate / "prepared.bin").write_bytes(b"prepared")
    Config.load(file)
    assert (config.candidate / "prepared.bin").read_bytes() == b"prepared"


@pytest.mark.parametrize(
    "settings",
    [
        '[recipe]\npath = "."',
        '[recipe]\npath = ".sisyphus"',
        "[recipe]\ntimeout = false",
        "unknown = 1",
        '[editor]\nmodel = "local"',
    ],
)
def test_invalid_settings_do_not_create_candidate(tmp_path, settings):
    file = tmp_path / "sisyphus.toml"
    file.write_text(settings)
    with pytest.raises(InfrastructureError):
        Config.load(file)
    assert not (tmp_path / "recipe").exists()
    assert not (tmp_path / ".sisyphus").exists()


def test_initial_recipe_rejects_symlink(tmp_path):
    file = tmp_path / "sisyphus.toml"
    file.write_text("[recipe]")
    (tmp_path / "recipe").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InfrastructureError, match="symbolic link"):
        Config.load(file)


def test_python_configuration_is_not_discovered_or_loaded(tmp_path):
    file = tmp_path / "test_goal.py"
    file.write_text('SISYPHUS = {}\nraise RuntimeError("must not execute")')
    assert discover(file) is None
    with pytest.raises(InfrastructureError, match="Cannot read"):
        Config.load(file)
    assert not (tmp_path / "recipe").exists()
