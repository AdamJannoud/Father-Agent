"""The CLI: --help, doctor, new, list, show, providers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from father_agent.cli import main

from .conftest import ROOT, SAMPLE_COMMAND


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    text = capsys.readouterr().out
    for command in ("new", "list", "show", "providers", "doctor"):
        assert command in text


def test_main_py_help_runs_as_a_script() -> None:
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "doctor" in proc.stdout


def test_doctor_reports_provider_and_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "groq" in text and "huggingface" in text
    assert "offline mock" in text
    assert "free-only" in text and "prompts" in text
    assert "result: ready" in text


def test_doctor_reports_bad_config(monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("HF_BASE_URL", "https://api.openai.com/v1")
    assert main(["doctor"]) == 1
    assert "only free" in capsys.readouterr().out
    assert main(["list"]) == 2


def test_new_list_show_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "agents"
    assert main(["new", "-o", str(out), SAMPLE_COMMAND]) == 0
    text = capsys.readouterr().out
    assert "father validate 4/6" in text and "nothing was executed" in text
    assert (out / "wallet_watcher" / "agent.py").is_file()

    assert main(["list", "-o", str(out)]) == 0
    assert "wallet_watcher" in capsys.readouterr().out

    assert main(["show", "wallet_watcher", "-o", str(out)]) == 0
    shown = capsys.readouterr().out
    assert "WalletWatcher" in shown and "policy ok" in shown

    # generate is an alias, and an existing folder is a clean failure
    assert main(["generate", "-o", str(out), SAMPLE_COMMAND]) == 1
    assert "already exists" in capsys.readouterr().err


def test_new_without_text_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new"]) == 2
    assert "describe the sub-agent" in capsys.readouterr().err


def test_providers_lists_chain(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["providers"]) == 0
    assert "active chain: mock/offline-templates" in capsys.readouterr().out


def test_bot_module_entry_without_the_extra_gives_the_install_hint() -> None:
    """``python -m father_agent.bot`` on a core-only install: the CLI's hint, no traceback."""
    block = "import sys; sys.modules['aiogram'] = None; import runpy; sys.argv[1:] = ['--check']; "
    runs = {
        "module": block + "runpy.run_module('father_agent.bot', run_name='__main__')",
        "script": block.replace("['--check']", "['bot', '--check']")
                  + "runpy.run_path('main.py', run_name='__main__')",
    }
    procs = {name: subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                  text=True, timeout=60) for name, code in runs.items()}
    module, script = procs["module"], procs["script"]
    assert module.returncode == script.returncode == 2
    assert "requirements-bot.txt" in module.stderr and "Traceback" not in module.stderr
    assert module.stderr == script.stderr
