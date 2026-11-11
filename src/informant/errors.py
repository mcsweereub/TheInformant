"""Error types with stable codes, so other programs can react to failures.

Each error has a machine-readable `code` (used in `--json` output) and the
process `exit_code` the CLI exits with:

    0  success
    1  internal_error   unexpected bug
    2  not_found        no company matches the query
    3  config_error     missing/invalid settings (API key, model, bad option)
    4  upstream_error   a remote service failed (Companies House, network)
"""

from __future__ import annotations


class InformantError(Exception):
    code = "internal_error"
    exit_code = 1


class NotFoundError(InformantError):
    code = "not_found"
    exit_code = 2


class ConfigError(InformantError):
    code = "config_error"
    exit_code = 3


class UpstreamError(InformantError):
    code = "upstream_error"
    exit_code = 4
