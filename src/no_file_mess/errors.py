"""Errors the command line reports without a traceback."""


class NoFileMessError(Exception):
    """Raised when a command cannot do what was asked.

    Unexpected bugs are left uncaught so they still show a traceback.
    """
