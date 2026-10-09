"""Run the test suites the mock coder generates.

The factory itself never executes generated code. This test does, on purpose,
to prove the templates are real, working sub-agents. A suite whose third-party
libraries are not installed skips itself via pytest.importorskip.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from father_agent.config import Config
from father_agent.factory import Factory

from .conftest import SAMPLE_COMMAND

COMMANDS = [
    SAMPLE_COMMAND,
    "scrape hacker news headlines hourly",
    "train a classifier on a churn csv dataset and chart the score",
    "organize my downloads folder",
    "track the bitcoin price API every 5 minutes",
]


@pytest.mark.parametrize("command", COMMANDS)
def test_generated_suite_passes(command: str, config: Config, tmp_path: Path) -> None:
    async def build():
        factory = Factory(config)
        try:
            return await factory.generate(command, output_dir=tmp_path)
        finally:
            await factory.aclose()

    result = asyncio.run(build())
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=result.target_dir, capture_output=True, text=True, timeout=120,
                          env=env)
    # 5 = nothing collected: the suite skipped itself because a library is missing
    skipped = proc.returncode == 5 and "skipped" in proc.stdout
    assert proc.returncode == 0 or skipped, proc.stdout + proc.stderr
    assert "passed" in proc.stdout or skipped
