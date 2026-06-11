"""CLI logging bootstrap — the single home of log-level wiring.

Library modules across tigerharness attach **named loggers only**
(``logging.getLogger("tigerharness.<package>.<module>")``) and never
call ``basicConfig``. Entry points — and only entry points — call
:func:`configure_cli_logging` once, so every CLI shares one parser
for the ``TIGERHARNESS_LOG_LEVEL`` environment variable, one default,
and one format. The slack-bridge daemon keeps its own richer handler
setup in ``slack_bridge/__main__.py`` (journald-aware); everything
else routes through here.

Destination: stderr (the ``logging`` default). For skill-driven
in-session runs the CLIs' stderr appears in the tool-call output,
which is exactly the AI-readable log surface the log map documents.

``default`` exists because one CLI is special: ``notify`` has always
run at INFO (its basicConfig predates this helper), so it passes
``default="INFO"`` to preserve behavior; everything else takes the
quiet WARNING default and opts in via the env var.
"""

from __future__ import annotations

import logging
import os

#: Environment variable read by every tigerharness CLI entry point.
ENV_VAR = "TIGERHARNESS_LOG_LEVEL"

_VALID = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}


def configure_cli_logging(default: str = "WARNING") -> int:
    """Configure root logging for a CLI entry point.

    Reads ``TIGERHARNESS_LOG_LEVEL`` (case-insensitive; one of
    CRITICAL/ERROR/WARNING/INFO/DEBUG). An unset or unrecognized
    value falls back to *default*. Returns the numeric level applied
    (handy for tests and for callers that branch on verbosity).
    """
    raw = os.environ.get(ENV_VAR, default).strip().upper()
    name = raw if raw in _VALID else default.strip().upper()
    level = getattr(logging, name)
    logging.basicConfig(
        level=level, format="%(levelname)s %(name)s: %(message)s"
    )
    return level
