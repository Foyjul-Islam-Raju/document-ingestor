"""Shared extraction exception types."""


class ExtractionError(Exception):
    """An expected, user-facing extraction failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

