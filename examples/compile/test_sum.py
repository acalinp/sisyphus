import pytest


@pytest.mark.parametrize(("left", "right"), [(17, 25), (-7, 3), (0, 0)])
def test_sum(recipe, left, right):
    """Build a C program that adds two integers, with reproducible user instructions."""
    result = recipe.run(["sh", "build.sh", str(left), str(right)])
    assert result.stdout.strip() == str(left + right)
    assert result.file("out/sum").read_bytes().startswith(b"\x7fELF")
    assert "sh build.sh" in result.file("README.md").read_text()
