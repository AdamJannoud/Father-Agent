"""The ledger: one JSON line per proposal and its decision, kept on disk.

A self-modifying tool needs a record that outlives the terminal, so every
proposal is written here whether it was applied, declined or refused.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .paths import EvolutionPaths


def record(paths: EvolutionPaths, *, task: str, pack: str, dependencies: list[str],
           decision: str, reason: str = "", diff_hash: str = "") -> dict[str, Any]:
    """Append one line and return it."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task": task,
            "pack": pack, "dependencies": dependencies, "decision": decision,
            "reason": reason, "diff_hash": diff_hash}
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    with paths.ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    return line


def read(paths: EvolutionPaths) -> list[dict[str, Any]]:
    """Every ledger line, oldest first; unreadable lines are skipped."""
    if not paths.ledger.is_file():
        return []
    lines: list[dict[str, Any]] = []
    for raw in paths.ledger.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    return lines
