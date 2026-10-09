"""The deployed build command stays wheel-only and two-step. Offline: it only parses files.

A Render deploy once died building aiohttp from source under Python 3.12
(`'PyLongObject' has no member named 'ob_digit'`). These checks keep the guard
that makes that impossible: an exact aiohttp pin with 3.12 wheels, and
`--only-binary` on its C stack so pip can never fall back to an sdist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS = ["render.yaml", "deploy/render-worker.yaml"]
WHEEL_ONLY = {"aiohttp", "multidict", "yarl", "frozenlist"}


def build_command(rel: str) -> str:
    data = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
    return data["services"][0]["buildCommand"]


def pip_installs(command: str) -> list[str]:
    return [step.strip() for step in command.split("&&") if " install " in f" {step} "]


@pytest.mark.parametrize("rel", BLUEPRINTS)
def test_requirement_files_install_in_separate_pip_steps(rel: str) -> None:
    # requirements.txt pins pydantic 2.14.0 and requirements-bot.txt 2.13.5 (aiogram
    # caps it below 2.14), so one pip command naming both is unsatisfiable.
    steps = pip_installs(build_command(rel))
    core = [s for s in steps if re.search(r"-r\s+requirements\.txt\b", s)]
    bot = [s for s in steps if re.search(r"-r\s+requirements-bot\.txt\b", s)]
    assert len(core) == 1 and len(bot) == 1, steps
    assert core[0] != bot[0], "both files in one pip command cannot resolve"
    assert steps.index(core[0]) < steps.index(bot[0]), "the bot extra goes on top of the core"


@pytest.mark.parametrize("rel", BLUEPRINTS)
def test_bot_stack_is_wheel_only(rel: str) -> None:
    command = build_command(rel)
    assert "--no-binary" not in command
    bot = next(s for s in pip_installs(command) if "requirements-bot.txt" in s)
    match = re.search(r"--only-binary[= ](\S+)", bot)
    assert match, f"{rel}: the requirements-bot.txt step has no --only-binary guard"
    named = set(match.group(1).split(","))
    assert ":all:" in named or WHEEL_ONLY <= named, f"missing {WHEEL_ONLY - named}"


def test_blueprints_share_one_build_command() -> None:
    first, second = (build_command(rel) for rel in BLUEPRINTS)
    assert first == second


def test_python_version_selects_312() -> None:
    path = ROOT / ".python-version"
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip().startswith("3.12")


def test_aiohttp_pinned_exactly_at_or_above_39() -> None:
    lines = (ROOT / "requirements-bot.txt").read_text(encoding="utf-8").splitlines()
    specs = [ln.split("#", 1)[0].strip() for ln in lines]
    pins = [s for s in specs if re.match(r"aiohttp\b", s, re.IGNORECASE)]
    assert len(pins) == 1, pins
    match = re.fullmatch(r"aiohttp\s*==\s*(\d+)\.(\d+)\.(\d+)", pins[0], re.IGNORECASE)
    assert match, f"aiohttp must be an exact == pin, got {pins[0]!r}"
    assert tuple(int(part) for part in match.groups()) >= (3, 9, 0)
