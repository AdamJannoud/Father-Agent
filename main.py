#!/usr/bin/env python3
"""Father Agent command-line entry point.

    python main.py --help
    python main.py doctor
    python main.py new "a Solana wallet watcher that logs balance changes every 60s"
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # noqa: UP036 - give a clear message on old interpreters
    sys.stderr.write("father: Python 3.11 or newer is required\n")
    raise SystemExit(2)

from father_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
