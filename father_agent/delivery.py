"""The delivery layer: how a sub-agent reaches its user and where it is hosted.

The planner decides an interface (``cli``, ``web``, ``telegram`` or ``api``);
when it does not, :func:`infer_delivery` reads one from the command, and the
CLI's ``--interface`` / ``--framework`` override both. :func:`apply_delivery`
then adds what that interface needs to the spec (its framework packages and
environment variables) before any code is written.

Everything here that becomes a file is **exact static text**, never model
output: ``requirements.txt``, ``.env.example``, ``bootstrap.py``, the
``Dockerfile``, ``.dockerignore``, ``scripts/setup_bot.py`` and one
self-contained folder per hosting target under ``deploy/``. The model writes
the logic; the factory writes the plumbing. That is also what lets the offline
mock produce a complete, deployable bundle.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from string import Template

from .errors import FatherAgentError
from .spec import (
    DEPLOY_TARGETS,
    FRAMEWORKS,
    INTERFACES,
    Delivery,
    Dependency,
    EnvVar,
    SubAgentSpec,
    import_name_for,
)

# --------------------------------------------------------------------------- #
# Interface: inferred from the sentence, overridable from the CLI
# --------------------------------------------------------------------------- #

TELEGRAM_WORDS = ("telegram", "aiogram", " tg bot")
WEB_WORDS = ("dashboard", "streamlit", "web app", "webapp", "web ui", "web interface",
             "web page that shows", "website that shows", "in the browser", "web front")
API_WORDS = ("fastapi", "microservice", "web service", "http service", "api server",
             "api service", "rest service", "json service")
_SERVE_RE = re.compile(r"\b(serve|serves|serving|expose|exposes|exposing)\b.*"
                       r"\b(api|endpoints?|http|json|rest)\b")


def infer_delivery(command: str) -> Delivery:
    """Read the interface from the command; ``cli`` when nothing signals one.

    "a Telegram bot that ..." is a telegram bot, "a dashboard that ..." is a
    Streamlit app, "a service that exposes ... over HTTP" is a FastAPI api.
    Consuming an API ("track the bitcoin price API") is not serving one, so
    it stays ``cli``.
    """
    text = f" {' '.join(command.lower().split())} "
    if any(word in text for word in TELEGRAM_WORDS):
        return Delivery(interface="telegram")
    if any(word in text for word in WEB_WORDS):
        return Delivery(interface="web", framework="fastapi" if "fastapi" in text else "")
    if any(word in text for word in API_WORDS) or _SERVE_RE.search(text):
        return Delivery(interface="api")
    return Delivery()


def choose_delivery(current: Delivery, *, interface: str | None = None,
                    framework: str | None = None) -> Delivery:
    """Apply ``--interface`` / ``--framework`` overrides to a delivery block.

    A framework given alone selects the interface it belongs to (``aiogram``
    means telegram). Changing the interface resets the framework and targets
    to that interface's defaults unless they were given too.

    Raises:
        FatherAgentError: For a framework that does not fit the interface.
    """
    if not interface and not framework:
        return current
    if framework and not interface:
        fits = [i for i in INTERFACES if framework in FRAMEWORKS[i]]
        if not fits:
            raise FatherAgentError(f"unknown framework {framework!r}")
        interface = current.interface if current.interface in fits else fits[0]
    assert interface is not None
    if interface not in INTERFACES:
        raise FatherAgentError(f"unknown interface {interface!r}")
    same = interface == current.interface
    try:
        return Delivery(interface=interface,
                        framework=framework or (current.framework if same else ""),
                        deploy=current.deploy if same else [])
    except ValueError as exc:
        raise FatherAgentError(str(exc).splitlines()[-1].strip()) from exc


#: Packages each framework needs, with why. They join the spec's dependencies.
FRAMEWORK_DEPENDENCIES: dict[str, tuple[tuple[str, str], ...]] = {
    "streamlit": (("streamlit", "the web dashboard"),
                  ("python-dotenv", "reads settings from .env")),
    "fastapi": (("fastapi", "the HTTP app"), ("uvicorn", "serves the app"),
                ("python-dotenv", "reads settings from .env")),
    "aiogram": (("aiogram", "the Telegram bot (aiogram 3 dispatcher)"),
                ("aiohttp", "the webhook server on a host"),
                ("python-dotenv", "reads settings from .env")),
}

#: Environment variables each interface reads, besides the agent's own.
INTERFACE_ENV: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "cli": (),
    "web": (("PORT", "port the app listens on (set by most hosts)", False),),
    "api": (("PORT", "port the app listens on (set by most hosts)", False),),
    "telegram": (
        ("BOT_TOKEN", "bot token from @BotFather; never commit it", True),
        ("TELEGRAM_CHAT_ID", "chat that always receives alerts (optional)", False),
        ("ALLOWED_USER_IDS", "comma-separated Telegram user ids allowed to use the bot",
         False),
        ("WEBHOOK_BASE_URL", "public https URL for webhook mode (Render sets "
                             "RENDER_EXTERNAL_URL instead)", False),
        ("WEBHOOK_SECRET", "secret Telegram echoes on every webhook call", False),
        ("PORT", "port of the webhook server", False),
    ),
}


def apply_delivery(spec: SubAgentSpec) -> SubAgentSpec:
    """Add the interface's framework packages and environment variables to ``spec``.

    Idempotent: running it twice adds nothing the second time. A cli spec is
    returned unchanged, so a spec with no delivery block behaves as before.
    """
    delivery = spec.delivery
    have = spec.import_names
    for package, purpose in FRAMEWORK_DEPENDENCIES.get(delivery.framework, ()):
        if import_name_for(package) not in have:
            spec.dependencies.append(Dependency(package=package, purpose=purpose))
            have.add(import_name_for(package))
    names = {e.name for e in spec.env_vars}
    for name, purpose, required in INTERFACE_ENV[delivery.interface]:
        if name not in names:
            spec.env_vars.append(EnvVar(name=name, purpose=purpose, required=required))
            names.add(name)
    return spec


def interface_file(spec: SubAgentSpec) -> str | None:
    """The file the interface adds: ``app.py``, ``bot.py``, or None for cli."""
    return {"web": "app.py", "api": "app.py", "telegram": "bot.py"}.get(
        spec.delivery.interface)


def entry_file(spec: SubAgentSpec) -> str:
    """The file a host runs: the interface file, or ``agent.py`` for cli."""
    return interface_file(spec) or "agent.py"


def local_port(spec: SubAgentSpec) -> int:
    """The port the app listens on locally and in the root Dockerfile."""
    if spec.delivery.framework == "streamlit":
        return 8501
    return {"web": 8000, "api": 8000, "telegram": 8080}.get(spec.delivery.interface, 0)


def next_command(spec: SubAgentSpec, folder: str) -> str:
    """The one line printed after a successful run."""
    delivery = spec.delivery
    if delivery.interface == "cli":
        return spec.run_example
    env = "cp .env.example .env && "
    run = {"streamlit": "streamlit run app.py", "fastapi": "python app.py",
           "aiogram": "python bot.py"}[delivery.framework]
    return f"cd {folder} && {env}{run}"


# --------------------------------------------------------------------------- #
# Dependencies: a pinned, complete requirements.txt
# --------------------------------------------------------------------------- #

#: Versions the factory pins when the planner gave none. Checked against PyPI
#: on 2026-10-09; edit freely, the gate only requires that every import is
#: covered, not that it is pinned to these.
KNOWN_PINS: dict[str, str] = {
    "aiogram": "3.31.0", "aiohttp": "3.14.4", "apscheduler": "3.11.3",
    "beautifulsoup4": "4.15.0", "discord.py": "2.7.1", "fastapi": "0.143.0",
    "httpx": "0.28.1", "huggingface-hub": "2.2.0", "jinja2": "3.1.6", "lxml": "6.1.3",
    "matplotlib": "3.11.2", "numpy": "2.5.3", "pandas": "3.0.6", "pillow": "12.3.0",
    "plotly": "7.1.0", "polars": "2.0.0", "pydantic": "2.13.5",
    "python-dateutil": "2.9.0.post0", "python-dotenv": "1.2.4",
    "python-telegram-bot": "22.8", "pyyaml": "6.0.3", "requests": "2.34.2",
    "scikit-learn": "1.9.1", "scipy": "1.18.1", "selectolax": "1.0.0", "solana": "0.41.0",
    "solders": "0.29.0", "streamlit": "1.65.0", "uvicorn": "0.54.0", "watchfiles": "1.3.0",
    "web3": "8.0.0",
}

_SPECIFIER_RE = re.compile(r"[<>=!~;\[ ]")


def requirement_name(line: str) -> str:
    """``pandas==3.0.6  # why`` -> ``pandas``; blank and comment lines -> ``""``."""
    text = line.split("#", 1)[0].strip()
    if not text or text.startswith("-"):
        return ""
    return _SPECIFIER_RE.split(text, maxsplit=1)[0].strip().lower()


def requirement_for(dep: Dependency) -> str:
    """One requirements.txt line: the planner's specifier, else a known pin."""
    package = dep.package.strip()
    if _SPECIFIER_RE.search(package):
        return package
    pin = KNOWN_PINS.get(package.lower())
    return f"{package}=={pin}" if pin else package


def render_requirements(spec: SubAgentSpec) -> str:
    """``requirements.txt`` for the bundle, generated from the spec's dependencies."""
    lines = [f"# {spec.name}: runtime dependencies, generated by the Father Agent from "
             f"spec.json.",
             "# Install: pip install -r requirements.txt   (or: python bootstrap.py "
             "--auto-install)"]
    width = max((len(requirement_for(d)) for d in spec.dependencies), default=0) + 2
    for dep in spec.dependencies:
        line = requirement_for(dep)
        note = dep.purpose or "runtime dependency"
        if not _SPECIFIER_RE.search(line):
            note += " (unpinned: not in the factory's pin table)"
        lines.append(f"{line:<{width}}# {note}")
    if not spec.dependencies:
        lines.append("# standard library only: nothing to install")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Environment: every variable the bundle reads
# --------------------------------------------------------------------------- #

_ENV_LINE_RE = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]{1,60})=(.*)$")


def env_example_entries(text: str) -> dict[str, tuple[bool, str]]:
    """``NAME -> (commented, value)`` for each ``NAME=`` or ``# NAME=`` line."""
    entries: dict[str, tuple[bool, str]] = {}
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line)
        if match:
            entries[match.group(2)] = (bool(match.group(1)), match.group(3).strip())
    return entries


_ENV_FUNCS = {("os", "getenv"), ("environ", "get"), ("os.environ", "get")}


def env_names_read(source: str) -> set[str]:
    """UPPER_SNAKE names a file reads with ``os.environ.get``/``os.getenv``/``os.environ[]``.

    Static: the source is parsed, never imported.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value)
            if (owner, node.func.attr) in _ENV_FUNCS or (
                    owner.endswith("environ") and node.func.attr in ("get", "setdefault")):
                key = node.args[0]
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
            key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                and re.fullmatch(r"[A-Z][A-Z0-9_]{1,60}", key.value):
            found.add(key.value)
    return found


#: Variables a host sets itself; listed in .env.example only as a comment.
HOST_SET = {"RENDER_EXTERNAL_URL", "SPACE_ID", "SPACE_HOST"}
#: What the variables the kit and templates read (but no spec declares) are for.
KNOWN_PURPOSES = {
    "FATHER_AUTO_INSTALL": "set to 1 to let bootstrap.py pip install missing declared packages",
    "HOST": "interface the app binds to (default 0.0.0.0)",
}


def render_env_example(spec: SubAgentSpec, code: dict[str, str]) -> str:
    """``.env.example``: every declared variable and every one the code reads, all empty."""
    declared = {e.name: e for e in spec.env_vars}
    read = set().union(*(env_names_read(src) for name, src in code.items()
                         if name.endswith(".py") and not name.startswith("test_")))
    lines = [f"# {spec.name}: copy to .env and fill in what you need. Never commit .env.",
             "# Required variables are live lines; optional ones are commented out so",
             "# their defaults apply. Uncomment a line to set it."]
    for name in [*declared, *sorted(read - set(declared) - HOST_SET)]:
        env = declared.get(name)
        purpose = env.purpose if env else KNOWN_PURPOSES.get(name, "read by the code; optional")
        if name.endswith("_AUTORUN") and not env:
            purpose = "set to 1 to run the agent on its schedule inside the app"
        if env and env.required:
            lines += ["", f"# {purpose} (required)", f"{name}="]
        else:
            lines += ["", f"# {purpose}", f"# {name}="]
    hosted = sorted(read & HOST_SET)
    if hosted:
        lines += ["", f"# Set by the host, not by you: {', '.join(hosted)}"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# bootstrap.py: opt-in install of missing declared packages
# --------------------------------------------------------------------------- #

_BOOTSTRAP = Template('''\
"""Check, and only when asked install, the packages ${name} declares.

    python bootstrap.py                    # report what is missing, install nothing
    python bootstrap.py --auto-install     # pip install the missing declared packages
    FATHER_AUTO_INSTALL=1 python bootstrap.py

Only requirements listed in requirements.txt are ever installed, never a name
the code merely happens to import. Inside the Docker image this is not needed:
the image is built from requirements.txt. Written by the Father Agent.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("bootstrap")

REQUIREMENTS = Path(__file__).resolve().with_name("requirements.txt")
#: pip name -> import name, for the declared packages whose two names differ.
IMPORT_NAMES: dict[str, str] = ${import_names}


def declared(path: Path = REQUIREMENTS) -> dict[str, str]:
    """Return ``requirement line -> import name`` for every line of requirements.txt."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.error("cannot read %s: %s", path, exc)
        return {}
    found: dict[str, str] = {}
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = line
        for mark in "<>=!~;[ ":
            name = name.split(mark, 1)[0]
        name = name.strip().lower()
        found[line] = IMPORT_NAMES.get(name, name.replace("-", "_"))
    return found


def missing(requirements: dict[str, str]) -> list[str]:
    """Requirement lines whose import name cannot be found in this interpreter."""
    return [line for line, module in requirements.items()
            if importlib.util.find_spec(module) is None]


def install(lines: list[str]) -> int:
    """pip install exactly ``lines`` with this interpreter; returns pip's exit code."""
    command = [sys.executable, "-m", "pip", "install", *lines]
    logger.info("running: %s", " ".join(command))
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        logger.error("could not run pip: %s", exc)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Report missing packages; install them only with --auto-install or FATHER_AUTO_INSTALL=1."""
    parser = argparse.ArgumentParser(description="check (and optionally install) the "
                                                 "declared dependencies")
    parser.add_argument("--auto-install", action="store_true",
                        help="pip install the declared packages that are missing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    wanted = missing(declared())
    if not wanted:
        logger.info("every declared package is installed")
        return 0
    logger.info("missing: %s", ", ".join(wanted))
    opted_in = args.auto_install or os.environ.get("FATHER_AUTO_INSTALL", "") == "1"
    if not opted_in:
        logger.info("nothing installed; re-run with --auto-install (or FATHER_AUTO_INSTALL=1)")
        return 1
    return install(wanted)


if __name__ == "__main__":
    raise SystemExit(main())
''')


def render_bootstrap(spec: SubAgentSpec) -> str:
    """``bootstrap.py``: standard library only, installs declared packages on request."""
    differ = {d.package.split("[")[0].strip().lower(): d.import_name for d in spec.dependencies
              if import_name_for(d.package) != d.package.strip().lower().replace("-", "_")
              or d.import_name != import_name_for(d.package)}
    return _BOOTSTRAP.substitute(name=spec.name, import_names=json.dumps(differ, sort_keys=True))


# --------------------------------------------------------------------------- #
# Docker
# --------------------------------------------------------------------------- #

_DOCKERFILE = Template('''\
# ${name}: container image, written by the Father Agent.
#   docker build -t ${image} .
#   docker run --rm --env-file .env${publish} ${image}
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1${extra_env}

# Run as an unprivileged user (uid 1000, which Hugging Face Spaces also expects).
RUN useradd --create-home --uid 1000 app && mkdir -p /app && chown app:app /app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
USER app
${expose}CMD ${cmd}
''')

_DOCKERIGNORE = '''\
# Keep secrets, history and caches out of the image.
.env
.env.*
!.env.example
*_data/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.venv/
venv/
.git/
deploy/
test_*.py
'''


def _cmd(spec: SubAgentSpec, port: int, *, webhook: bool = False) -> list[str]:
    """Exec-form CMD for a container listening on ``port``."""
    framework = spec.delivery.framework
    if framework == "streamlit":
        return ["sh", "-c", f"streamlit run app.py --server.port=${{PORT:-{port}}} "
                            f"--server.address=0.0.0.0 --server.headless=true"]
    if framework == "fastapi":
        return ["python", "app.py"]
    if framework == "aiogram":
        return ["python", "bot.py", *(["--webhook"] if webhook else [])]
    return ["python", "agent.py"]


def render_dockerfile(spec: SubAgentSpec, *, port: int | None = None) -> str:
    """A ``python:3.12-slim``, non-root Dockerfile whose CMD runs the entry file."""
    port = local_port(spec) if port is None else port
    extra = f" \\\n    PORT={port}" if port else ""
    expose = f"EXPOSE {port}\n" if port else ""
    publish = f" -p {port}:{port}" if port else ""
    return _DOCKERFILE.substitute(name=spec.name, image=spec.slug.replace("_", "-"),
                                  publish=publish, extra_env=extra, expose=expose,
                                  cmd=json.dumps(_cmd(spec, port)))


# --------------------------------------------------------------------------- #
# Telegram: scripts/setup_bot.py
# --------------------------------------------------------------------------- #

_SETUP_BOT = Template('''\
"""Prepare the ${name} Telegram bot: verify the token, register its command menu,
and optionally point Telegram at a webhook.

    python scripts/setup_bot.py                                  # getMe + command menu
    python scripts/setup_bot.py --webhook https://<app>.onrender.com
    python scripts/setup_bot.py --delete-webhook                 # back to polling

BOT_TOKEN (and WEBHOOK_SECRET, when set) are read from the environment or .env,
never from this file. Written by the Father Agent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import BOT_COMMANDS, WEBHOOK_PATH  # noqa: E402

logger = logging.getLogger("setup_bot")


async def setup(token: str, webhook: str, delete_webhook: bool) -> int:
    """Run getMe, setMyCommands and, when asked, setWebhook or deleteWebhook."""
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        logger.info("token ok: @%s (id %s)", me.username, me.id)
        await bot.set_my_commands([BotCommand(command=name, description=text)
                                   for name, text in BOT_COMMANDS])
        logger.info("command menu set: %s", ", ".join(f"/{name}" for name, _ in BOT_COMMANDS))
        if webhook:
            url = webhook.rstrip("/") + WEBHOOK_PATH
            await bot.set_webhook(url, secret_token=os.environ.get("WEBHOOK_SECRET") or None)
            logger.info("webhook set: %s", url)
        elif delete_webhook:
            await bot.delete_webhook()
            logger.info("webhook deleted; the bot can poll again")
    except TelegramAPIError as exc:
        logger.error("Telegram refused the request: %s", exc)
        return 1
    finally:
        await bot.session.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the flags, read BOT_TOKEN and run the setup."""
    parser = argparse.ArgumentParser(description="verify the bot token and register commands")
    parser.add_argument("--webhook", default="", help="public https base URL to set as webhook")
    parser.add_argument("--delete-webhook", action="store_true", help="remove the webhook")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        logger.error("set BOT_TOKEN (from @BotFather) in .env or the environment")
        return 2
    return asyncio.run(setup(token, args.webhook, args.delete_webhook))


if __name__ == "__main__":
    raise SystemExit(main())
''')


# --------------------------------------------------------------------------- #
# Hosting targets: one self-contained folder each under deploy/
# --------------------------------------------------------------------------- #

#: Folder under deploy/ for each hosting target (docker uses the bundle root).
TARGET_FOLDERS = {"hf-spaces": "deploy/huggingface", "render": "deploy/render"}
#: Port Hugging Face Spaces routes to (``app_port``).
HF_PORT = 7860
#: Root files copied into each deploy folder, so the folder is a whole repository.
_HF_README = Template('''\
---
title: ${title}
emoji: ${emoji}
colorFrom: blue
colorTo: gray
sdk: docker
app_port: ${port}
pinned: false
short_description: ${short}
---

# ${title}

${summary}

This folder is a complete Hugging Face Space (Docker SDK), written by the
Father Agent. Push it as its own repository:

```bash
# 1. create an empty Space (SDK: Docker) at https://huggingface.co/new-space
# 2. then, from this folder:
git init -b main && git add . && git commit -m "Deploy ${slug}"
git remote add space https://huggingface.co/spaces/<your-user>/${space}
git push space main
```

Set the variables from `.env.example` under the Space's
*Settings → Variables and secrets*. Nothing secret is stored in this folder.
''')

_RENDER_YAML = Template('''\
# ${name} on Render (free web service), written by the Father Agent.
# Push this folder as its own repository, then New → Blueprint on
# https://dashboard.render.com and pick it. Render asks for every value marked
# "sync: false"; nothing secret is stored here.
services:
  - type: web
    name: ${service}
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: ${start}
    healthCheckPath: ${health}
    envVars:
      - key: PYTHON_VERSION
        value: "3.12.8"
${env}''')


def _render_env_lines(spec: SubAgentSpec) -> str:
    """envVars entries: required ones prompt (sync: false); the webhook secret is generated."""
    lines: list[str] = []
    for env in spec.env_vars:
        if env.name == "WEBHOOK_SECRET":
            lines += [f"      - key: {env.name}", "        generateValue: true"]
        elif env.required:
            lines += [f"      - key: {env.name}", "        sync: false"]
    return "".join(f"{line}\n" for line in lines)


def _render_start(spec: SubAgentSpec) -> tuple[str, str]:
    """Render ``startCommand`` and ``healthCheckPath`` for the interface."""
    framework = spec.delivery.framework
    if framework == "streamlit":
        return ("streamlit run app.py --server.port $PORT --server.address 0.0.0.0 "
                "--server.headless true", "/_stcore/health")
    if framework == "aiogram":
        return "python bot.py --webhook", "/health"
    return "python app.py", "/health"


def deploy_sources(spec: SubAgentSpec) -> tuple[str, ...]:
    """Root files each deploy folder carries a copy of."""
    return ("agent.py", entry_file(spec), "requirements.txt") if interface_file(spec) \
        else ("agent.py", "requirements.txt")


def render_target(spec: SubAgentSpec, target: str, code: dict[str, str]) -> dict[str, str]:
    """Files of one hosting target's folder (paths relative to the bundle root)."""
    folder = TARGET_FOLDERS[target]
    files = {f"{folder}/{name}": code[name] for name in deploy_sources(spec)}
    summary = " ".join(spec.summary.split())
    if target == "hf-spaces":
        short = summary if len(summary) <= 60 else summary[:57] + "..."
        files[f"{folder}/README.md"] = _HF_README.substitute(
            title=spec.name, emoji={"web": "📈", "api": "🔌"}.get(spec.delivery.interface, "🤖"),
            port=HF_PORT, short=json.dumps(short, ensure_ascii=False), summary=summary,
            slug=spec.slug, space=spec.slug.replace("_", "-"))
        files[f"{folder}/Dockerfile"] = render_dockerfile(spec, port=HF_PORT)
    elif target == "render":
        start, health = _render_start(spec)
        files[f"{folder}/render.yaml"] = _RENDER_YAML.substitute(
            name=spec.name, service=spec.slug.replace("_", "-"), start=start, health=health,
            env=_render_env_lines(spec))
    return files


def render_kit(spec: SubAgentSpec, code: dict[str, str]) -> dict[str, str]:
    """Every static delivery file for ``spec``, given the generated code files.

    Returns ``relative path -> text``. ``code`` must hold agent.py and, for a
    non-cli interface, its app.py or bot.py; the deploy folders copy them.
    """
    files = {
        "requirements.txt": render_requirements(spec),
        "bootstrap.py": render_bootstrap(spec),
        "Dockerfile": render_dockerfile(spec),
        ".dockerignore": _DOCKERIGNORE,
    }
    if spec.delivery.interface == "telegram":
        files["scripts/setup_bot.py"] = _SETUP_BOT.substitute(name=spec.name)
    files[".env.example"] = render_env_example(spec, {**code, **files})
    sources = {**code, "requirements.txt": files["requirements.txt"]}
    for target in spec.delivery.deploy:
        if target in TARGET_FOLDERS:
            files.update(render_target(spec, target, sources))
    return files


def kit_summary(files: Iterable[str]) -> str:
    """``Dockerfile · render.yaml · .env.example ...`` for the delivery progress line."""
    order = ["Dockerfile", ".dockerignore", "render.yaml", "deploy/huggingface/", ".env.example",
             "bootstrap.py", "requirements.txt", "scripts/setup_bot.py"]
    names = set(files)
    shown = [n for n in order if n in names or any(f.endswith("/" + n) for f in names)
             or (n.endswith("/") and any(f.startswith(n) for f in names))]
    return " · ".join(shown)


def describe_targets(spec: SubAgentSpec) -> str:
    """``docker, hf-spaces, render`` for display."""
    return ", ".join(spec.delivery.deploy) or ", ".join(DEPLOY_TARGETS["cli"])
