class CoreError(Exception):
    """Base for errors whose message is safe to show to the user as-is."""


class SettingsError(CoreError, ValueError):
    """Settings failed validation. `problems` lists every issue found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class PlanningError(CoreError):
    pass


class GenerationError(CoreError):
    pass


class ImageSearchError(CoreError):
    pass


class ImageDownloadError(CoreError):
    """An image address was refused or could not be downloaded."""
