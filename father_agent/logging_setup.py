"""Logging for the Father Agent.

Two destinations:

* ``logs/father-agent.log`` — every step, at DEBUG or the configured level,
  rotated so it never grows without bound.
* the terminal — the short ``father <stage> n/6 ...`` progress lines on stdout
  (logger ``father.progress``) and warnings/errors on stderr.

A redaction filter strips configured API keys from every record before it is
written anywhere, so a key cannot leak into a log file that later gets shared.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config

LOG_FILE_NAME = "father-agent.log"
PROGRESS_LOGGER = "father.progress"
_MANAGED = "_father_managed"


class RedactingFilter(logging.Filter):
    """Replace any configured secret in a log record with ``[REDACTED]``."""

    def __init__(self, secrets: list[str]) -> None:
        """Remember the secrets to scrub (empty strings are ignored)."""
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub the formatted message in place; always keep the record."""
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        return True


class _ProgressFormatter(logging.Formatter):
    """Format progress records as ``father <stage> <step>/6 <message>``."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the stage columns when present, else the bare message."""
        stage = getattr(record, "stage", "")
        step = getattr(record, "step", "")
        if stage:
            prefix = f"father {stage:<9}{step + '/6' if step else '':<5}"
            return f"{prefix}{record.getMessage()}"
        return record.getMessage()


class _LiveStreamHandler(logging.StreamHandler):
    """A StreamHandler that always writes to the *current* sys.stdout/stderr.

    Holding a reference to the stream at setup time breaks when the stream is
    later replaced (pytest capture, IDE consoles, redirected output).
    """

    def __init__(self, name: str) -> None:
        """``name`` is ``"stdout"`` or ``"stderr"``."""
        self._stream_name = name
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        """The current stream for this handler."""
        return getattr(sys, self._stream_name)

    @stream.setter
    def stream(self, _value: object) -> None:
        """Ignore assignment; the stream is always looked up live."""


def _mark(handler: logging.Handler) -> logging.Handler:
    """Tag a handler so a later setup_logging call can replace it."""
    setattr(handler, _MANAGED, True)
    return handler


def setup_logging(config: Config, *, verbose: bool = False, quiet: bool = False) -> Path | None:
    """Configure the ``father_agent`` and ``father`` loggers.

    Safe to call more than once: handlers from a previous call are removed.

    Args:
        config: Settings carrying the log directory, level and secrets.
        verbose: Mirror DEBUG records to stderr.
        quiet: Suppress progress lines on stdout.

    Returns:
        The log file path, or None if the log directory could not be created.
    """
    redactor = RedactingFilter(config.secrets)
    level = getattr(logging, config.log_level, logging.INFO)

    package_logger = logging.getLogger("father_agent")
    progress_logger = logging.getLogger(PROGRESS_LOGGER)
    for lg in (package_logger, progress_logger, logging.getLogger("father")):
        for handler in list(lg.handlers):
            if getattr(handler, _MANAGED, False):
                lg.removeHandler(handler)
                handler.close()

    package_logger.setLevel(logging.DEBUG)
    package_logger.propagate = False
    progress_logger.setLevel(logging.INFO)
    progress_logger.propagate = False

    log_path: Path | None = config.log_dir / LOG_FILE_NAME
    try:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3,
                                           encoding="utf-8")
        file_handler.setLevel(min(level, logging.DEBUG) if verbose else level)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        file_handler.addFilter(redactor)
        package_logger.addHandler(_mark(file_handler))
        progress_logger.addHandler(file_handler)
    except OSError as exc:
        log_path = None
        sys.stderr.write(f"father: could not open log file in {config.log_dir}: {exc}\n")

    console = _LiveStreamHandler("stderr")
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("father %(levelname)s: %(message)s"))
    console.addFilter(redactor)
    console.addFilter(lambda record: getattr(record, "console", True))
    package_logger.addHandler(_mark(console))

    if not quiet:
        progress = _LiveStreamHandler("stdout")
        progress.setLevel(logging.INFO)
        progress.setFormatter(_ProgressFormatter())
        progress.addFilter(redactor)
        progress_logger.addHandler(_mark(progress))

    package_logger.debug("logging configured: level=%s file=%s", config.log_level, log_path)
    return log_path


class ProgressReporter:
    """Emit the six-stage progress lines (spec, code, validate, delivery, write)."""

    def __init__(self) -> None:
        """Bind to the progress logger."""
        self._log = logging.getLogger(PROGRESS_LOGGER)

    def step(self, stage: str, step: int | None, message: str) -> None:
        """Log one progress line, e.g. ``step("code", 3, "wrote agent.py")``."""
        self._log.info(message, extra={"stage": stage, "step": str(step) if step else ""})

    def note(self, message: str) -> None:
        """Log a free-form line without stage columns."""
        self._log.info(message)
