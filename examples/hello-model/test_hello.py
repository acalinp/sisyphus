def test_greeting(recipe):
    """Produce a recipe that prints exactly 'hello, world'."""
    result = recipe.run(["sh", "hello"])
    assert result.stdout.strip() == "hello, world"
