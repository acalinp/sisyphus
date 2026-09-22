class InfrastructureError(RuntimeError):
    """The experiment could not be conducted reliably; do not ask for a repair."""


class RecipeRejected(AssertionError):
    """A recipe command failed to achieve the requested behavior."""
