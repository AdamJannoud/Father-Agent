"""The consent gate: fail-closed, default No.

* ``CI`` set to a true value: declined, even with ``--yes``.
* ``--yes``: approved (scripted runs on a developer machine).
* no interactive terminal on stdin (pipe, cron): declined.
* otherwise ask ``[y/N]``; only ``y`` or ``yes`` approves. Empty, EOF or
  anything else declines.

There is no "remember my answer": standing consent would stop being a gate.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TextIO

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Decision:
    """The gate's answer and why."""

    approved: bool
    reason: str


def in_ci(env: Mapping[str, str] | None = None) -> bool:
    """True when ``CI`` is set to a true value (GitHub Actions sets ``CI=true``)."""
    return (env if env is not None else os.environ).get("CI", "").strip().lower() in _TRUE


def ask(question: str, *, assume_yes: bool = False, stdin: TextIO | None = None,
        stdout: TextIO | None = None, env: Mapping[str, str] | None = None) -> Decision:
    """Ask ``question [y/N]`` and return the decision. Never raises on bad input."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if in_ci(env):
        return Decision(False, "CI=true: consent cannot be given in CI, not even with --yes")
    if assume_yes:
        return Decision(True, "--yes")
    try:
        interactive = stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        return Decision(False, "no terminal to confirm on")
    stdout.write(f"{question} [y/N] ")
    stdout.flush()
    try:
        answer = stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        answer = ""
    if not answer:
        stdout.write("\n")
    if answer.strip().lower() in ("y", "yes"):
        return Decision(True, "confirmed at the terminal")
    return Decision(False, "answered no" if answer.strip() else "no answer (default is No)")
