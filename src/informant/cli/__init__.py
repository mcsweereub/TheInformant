"""Command line interface.

People get readable output. Programs (e.g. a GUI) add `--json` to any command:
  * stdout carries exactly one JSON object: {"ok": true, ...} or
    {"ok": false, "error": {"code": ..., "message": ...}}
  * nothing ever prompts for input
  * the exit code says what happened (see errors.py): 0 ok, 1 internal error,
    2 not found, 3 config error, 4 upstream error
`lookup --progress` also writes one JSON progress event per line to stderr.
"""

# Importing the command modules registers their commands on `app`, in this order in --help.
# isort: off
from . import lookup  # noqa: F401
from . import data  # noqa: F401
from . import settings  # noqa: F401
# isort: on
from .common import app, err, out, print_json
from .settings import save_model_choice

__all__ = ["app", "err", "out", "print_json", "save_model_choice"]
