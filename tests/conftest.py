import json
from pathlib import Path

import pytest

from sisyphus.config import Config

pytest_plugins = ["pytester"]


@pytest.fixture
def project(tmp_path):
    def make(*, setup=None, references=None, image="docker.io/library/ubuntu:24.04"):
        candidate = tmp_path / "recipe"
        candidate.mkdir(exist_ok=True)
        file = tmp_path / "sisyphus.toml"
        text = f'[recipe]\npath = "recipe"\nimage = {json.dumps(image)}\n'
        if setup:
            text += f"setup = {json.dumps(setup)}\n"
        if references:
            text += "[references]\n"
            for name, path in references.items():
                text += f"{name} = {json.dumps(str(Path(path)))}\n"
        file.write_text(text)
        return Config.load(file)

    return make
