"""The status-message reporter and the optional-extra guard. Runs without aiogram."""

from __future__ import annotations

import asyncio
import builtins
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from father_agent.cli import main
from father_agent.config import Config
from father_agent.factory import Factory
from father_agent.logging_setup import PROGRESS_LOGGER, ProgressReporter
from father_agent.telegram_progress import TelegramProgress, elapsed_label

from .conftest import ROOT, SAMPLE_COMMAND


def test_progress_is_a_progress_reporter_and_still_logs(
        caplog: pytest.LogCaptureFixture) -> None:
    progress = TelegramProgress()
    assert isinstance(progress, ProgressReporter)
    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        progress.step("spec", 1, "planning · provider mock")  # no run yet: logs only
    assert [r.getMessage() for r in caplog.records] == ["planning · provider mock"]
    assert progress.lines() == []


def test_one_line_per_stage_and_coalesced_edits() -> None:
    edits: list[str] = []

    async def body() -> TelegramProgress:
        progress = TelegramProgress(min_interval=0.05)

        async def edit(text: str) -> None:
            edits.append(text)

        progress.begin(edit)
        progress.step("spec", 1, "planning · provider groq")
        await asyncio.sleep(0)  # the flusher draws once, then waits min_interval
        progress.step("code", 3, "wrote agent.py · 118 lines")
        progress.step("code", 3, "wrote test_agent.py · 40 lines")
        progress.step("validate", 4, "ruff ok · imports ok")
        progress.step("validate", 4, "secrets ok · nothing was executed")
        progress.note("next: python -m subagents.x.agent --once")
        await progress.finish("done")
        return progress

    progress = asyncio.run(body())
    # four lines arrived inside one interval: one working draw, then the final state
    assert len(edits) == 2 and progress.edits == 2
    assert edits[0] == "<b>working · 0:00</b>\n<pre>father 1/6 planning · provider groq</pre>"
    assert edits[-1].startswith("<b>done · 0:00</b>\n<pre>")
    assert progress.lines() == [
        "father 1/6 planning · provider groq",
        "father 3/6 wrote agent.py · 118 lines · wrote test_agent.py · 40 lines",
        "father 4/6 ruff ok · imports ok · secrets ok · nothing was executed",
    ]
    assert progress.next_command == "python -m subagents.x.agent --once"
    assert not progress.active


def test_failed_step_and_html_escaping() -> None:
    progress = TelegramProgress()
    progress.begin(lambda text: asyncio.sleep(0))
    assert progress.failed_step == 1
    progress._lines = {1: ["a"], 2: ["<slug> & co"]}
    progress._step = 2
    assert progress.failed_step == 3  # the plan is done, so coding was under way
    assert "&lt;slug&gt; &amp; co" in progress.render()
    assert elapsed_label(0) == "0:00" and elapsed_label(187.9) == "3:07"


def test_a_failing_edit_never_stops_the_factory(config: Config, tmp_path: Path) -> None:
    async def edit(text: str) -> None:
        raise ConnectionError("telegram went away")

    async def body():
        progress = TelegramProgress(min_interval=0)
        progress.begin(edit)
        factory = Factory(config, reporter=progress)
        try:
            result = await factory.generate(SAMPLE_COMMAND, output_dir=tmp_path / "out")
        finally:
            await factory.aclose()
        await progress.finish("done")
        return result, progress

    result, progress = asyncio.run(body())
    assert result.written and progress.edits == 0
    assert [line.split()[1] for line in progress.lines()] == [f"{n}/6" for n in range(1, 7)]


def test_bot_command_without_the_extra_explains_the_install(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_import = builtins.__import__

    def no_aiogram(name, *args, **kwargs):
        if name == "aiogram" or name.startswith("aiogram."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    import father_agent

    monkeypatch.delitem(sys.modules, "father_agent.bot", raising=False)
    monkeypatch.delattr(father_agent, "bot", raising=False)  # as on a core-only install
    monkeypatch.setattr(builtins, "__import__", no_aiogram)
    assert main(["bot"]) == 2
    text = capsys.readouterr().err
    assert "optional bot extra" in text and "pip install -r requirements-bot.txt" in text


def test_core_cli_never_imports_aiogram() -> None:
    code = ("import sys; import father_agent.cli, father_agent.factory; "
            "sys.exit(1 if 'aiogram' in sys.modules or 'father_agent.bot' in sys.modules "
            "else 0)")
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_doctor_reports_the_bot_extra_without_failing(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "telegram" in text
    assert "bot extra not installed" in text or "python main.py bot" in text
