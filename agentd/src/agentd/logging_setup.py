"""Logging: human-readable console output for the whole runtime.

The event *journal* (journal.py) is the machine-readable record; this module
only configures operator-facing logs. Every module logs under the
``agentd.*`` namespace.
"""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """Configure the ``agentd`` logger tree exactly once (idempotent)."""
    root = logging.getLogger("agentd")
    if root.handlers:  # already configured (tests, repeated CLI calls)
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"agentd.{name}")
