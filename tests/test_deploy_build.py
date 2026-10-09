"""The deployed build stays installable, both ways in. Offline: it only reads files.

A Render deploy once died building aiohttp from source under Python 3.12
(`'PyLongObject' has no member named 'ob_digit'`). These checks keep the guard
that makes that impossible: an exact aiohttp pin with 3.12 wheels, and
`--only-binary` on its C stack so pip can never fall back to an sdist.

They also guard the packaging path, which is a second way into this repo.
`pip install .[bot]` on a fresh clone used to die inside setuptools' discovery
("Multiple top-level packages discovered in a flat-layout: ['deploy', 'prompts',
'father_agent']"), and the metadata has to stay resolvable and complete for an
installed copy to actually work rather than merely build.
"""

from __future__ import annotations

import json
import re
import tomllib
from fnmatch import fnmatch
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


# --- the packaging path: `pip install .` and `pip install .[bot]` -------------


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def core_dependencies() -> list[str]:
    return pyproject()["project"]["dependencies"]


def bot_extra() -> list[str]:
    return pyproject()["project"]["optional-dependencies"]["bot"]


def requirement_lines(rel: str) -> list[str]:
    """The requirement specs in a requirements file, comments stripped."""
    specs = []
    for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
        spec = line.split("#", 1)[0].strip()
        if spec and not spec.startswith("-"):
            specs.append(spec)
    return specs


def package_name(spec: str) -> str:
    return re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0].strip().lower()


def version_clauses(spec: str) -> list[str]:
    """The operator clauses of a spec, its package name and marker removed."""
    text = re.sub(r"^[A-Za-z0-9_.\-]+(\[[^\]]*\])?", "", spec.split(";", 1)[0]).strip()
    return [clause.strip() for clause in text.split(",") if clause.strip()]


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text))


#: The version operators ``satisfies`` understands. Anything else raises, so an
#: operator the metadata grows later cannot slip past these guards unread.
VERSION_OPS = {
    "==": lambda a, b: a == b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def satisfies(version: str, spec: str) -> bool:
    """True when every clause of ``spec`` accepts ``version``.

    A predicate, not an assertion: the guards below need both directions (can
    this spec reach 3.8.6? does it accept 2.13.5?), so a clause that rejects the
    version returns False instead of raising.
    """
    got = version_tuple(version)
    clauses = version_clauses(spec)
    assert clauses, f"{spec!r} pins no version at all"
    for clause in clauses:
        match = re.fullmatch(r"(==|>=|<=|>|<)\s*(\d+(?:\.\d+)*)", clause)
        assert match, f"unsupported version clause {clause!r}: extend VERSION_OPS"
        want = version_tuple(match.group(2))
        size = max(len(got), len(want))
        left = got + (0,) * (size - len(got))
        right = want + (0,) * (size - len(want))
        if not VERSION_OPS[match.group(1)](left, right):
            return False
    return True


def aiohttp_declarations() -> list[tuple[str, str]]:
    """Every place aiohttp's version is decided, and every place it reaches a deploy."""
    found = [
        (f"requirements-bot.txt: {spec}", spec)
        for spec in requirement_lines("requirements-bot.txt")
        if package_name(spec) == "aiohttp"
    ]
    found += [
        (f"pyproject.toml [bot]: {spec}", spec)
        for spec in bot_extra()
        if package_name(spec) == "aiohttp"
    ]
    from father_agent.delivery import KNOWN_PINS

    # The pin a generated sub-agent inherits into its own requirements.txt.
    pin = f"aiohttp=={KNOWN_PINS['aiohttp']}"
    found.append(("father_agent/delivery.py KNOWN_PINS", pin))
    # A capability pack installs through requirements-packs.txt, which `evolve`
    # appends to; the pack's spec comes from this catalogue.
    catalogue = json.loads(
        (ROOT / "father_agent/evolution/catalogue.json").read_text(encoding="utf-8")
    )
    for entry in catalogue["entries"]:
        for dep in entry.get("dependencies", []):
            if dep["package"].lower() == "aiohttp":
                found.append((f"catalogue pack {entry['name']}", f"aiohttp{dep['version']}"))
    return found


def test_no_aiohttp_declaration_can_resolve_below_39() -> None:
    # 3.8.6 is the release that compiles C touching PyLongObject.ob_digit, which
    # Python 3.12 removed. No declaration anywhere may be able to pick it.
    declarations = aiohttp_declarations()
    assert len(declarations) >= 3, declarations
    for where, spec in declarations:
        assert not satisfies("3.8.6", spec), (
            f"{where} can still resolve aiohttp 3.8.6, which fails to build on Python "
            "3.12 ('PyLongObject' has no member named 'ob_digit')"
        )


def test_pyproject_names_the_one_real_package() -> None:
    # A flat checkout with deploy/ and prompts/ at the top level makes setuptools'
    # auto-discovery refuse, which fails `pip install .` on a fresh clone.
    include = pyproject()["tool"]["setuptools"]["packages"]["find"]["include"]
    assert include == ["father_agent*"], include
    assert fnmatch("father_agent", include[0])
    assert fnmatch("father_agent.evolution.applier", include[0])
    assert (ROOT / "father_agent" / "__init__.py").is_file()


def test_core_metadata_declares_every_core_requirement() -> None:
    # `pip install .[bot]` must leave a working agent, not just a built wheel.
    declared = {package_name(spec) for spec in core_dependencies()}
    required = {package_name(spec) for spec in requirement_lines("requirements.txt")}
    missing = sorted(required - declared)
    assert not missing, f"[project] dependencies do not declare {missing}"


def test_bot_extra_mirrors_requirements_bot_txt() -> None:
    extra = {package_name(spec): spec for spec in bot_extra()}
    pinned = {package_name(spec): spec for spec in requirement_lines("requirements-bot.txt")}
    assert extra == pinned, f"[bot] vs requirements-bot.txt: {extra} vs {pinned}"


def test_core_and_bot_pydantic_specifiers_intersect() -> None:
    # `pip install .[bot]` resolves the core dependencies and the extra together.
    # The core wants 2.14.0 and aiogram caps pydantic below 2.14, so the install
    # resolves only while the core's specifier accepts the extra's exact pin. If
    # this fails, `pip install .[bot]` stops resolving at all.
    core = next(spec for spec in core_dependencies() if package_name(spec) == "pydantic")
    bot = next(spec for spec in bot_extra() if package_name(spec) == "pydantic")
    clauses = version_clauses(bot)
    assert len(clauses) == 1 and clauses[0].startswith("=="), bot
    assert satisfies(clauses[0][2:], core), f"the bot's {bot!r} fails the core's {core!r}"


def test_the_wheel_carries_the_evolution_data() -> None:
    # A wheel must carry the JSON the evolution module reads from inside the
    # package, or `evolve` is broken in an installed copy.
    patterns = pyproject()["tool"]["setuptools"]["package-data"]["father_agent"]
    shipped = [
        path.relative_to(ROOT / "father_agent").as_posix()
        for path in sorted((ROOT / "father_agent").rglob("*"))
        if path.is_file()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
        and path.name != "ledger.jsonl"  # local self-evolution state, gitignored
    ]
    assert shipped, "no package data found: this check has gone stale"
    missing = [rel for rel in shipped if not any(fnmatch(rel, pattern) for pattern in patterns)]
    assert not missing, f"package-data does not ship {missing}"
