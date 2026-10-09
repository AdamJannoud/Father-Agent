"""Command-line interface: ``new``, ``list``, ``show``, ``deploy``, ``capabilities``,
``evolve``, ``bot``, ``providers``, ``doctor``.

``main.py`` at the repository root is a thin wrapper around :func:`main`.
Exit codes: 0 success, 1 the factory failed, 2 bad usage or configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import FREE_HOSTS, KNOWN_PROVIDERS, PROJECT_ROOT, Config, mask
from .delivery import (
    TARGET_FOLDERS,
    deploy_sources,
    expected_files,
    render_target,
    target_commands,
)
from .errors import ConfigError, FatherAgentError
from .evolution import (
    EvolutionError,
    EvolutionPaths,
    capability_lines,
    evolve,
    ledger_lines,
    planned_spec,
)
from .factory import BUNDLE_FILES, LEGACY_FILES, Factory, load_spec
from .logging_setup import LOG_FILE_NAME, setup_logging
from .prompts import PromptLibrary
from .providers.chain import build_chain
from .spec import DEPLOY_TARGETS, FRAMEWORKS, INTERFACES, SubAgentSpec
from .validator import Validator, find_ruff, gate_summary

logger = logging.getLogger(__name__)

EXAMPLE = 'python main.py new "a Solana wallet watcher that logs balance changes every 60s ' \
          'and plots them"'


def out(text: str = "") -> None:
    """Write one line of command output to stdout."""
    sys.stdout.write(text + "\n")


def err(text: str) -> None:
    """Write one line of error output to stderr."""
    sys.stderr.write(f"father: {text}\n")


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="The Father Agent: type one line of English, get a planned, written and "
                    "validated async Python sub-agent. Free inference only (Groq free tier, "
                    "Hugging Face); an offline mock runs when no key is set.",
        epilog=f"example:\n  {EXAMPLE}\n  python main.py doctor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"father-agent {__version__}")
    parser.add_argument("--env-file", type=Path, help="read settings from this .env file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output on stderr")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    new = sub.add_parser("new", aliases=["generate"],
                         help="plan, write and validate a sub-agent from a text command",
                         description="Plan, write and validate a sub-agent. Generated code is "
                                     "never executed; files are written only after the "
                                     "validator gate passes.")
    new.add_argument("text", nargs="*", help="what the sub-agent should do (quote it)")
    new.add_argument("-o", "--out", type=Path, help="parent output folder (default: subagents/)")
    new.add_argument("-p", "--provider", choices=("auto", *KNOWN_PROVIDERS), default="auto",
                     help="force one provider (default: auto — keyed providers, else mock)")
    new.add_argument("--from-spec", type=Path, metavar="SPEC",
                     help="rebuild from an existing spec.json instead of planning")
    new.add_argument("-i", "--interface", choices=INTERFACES,
                     help="override the interface read from the command (default: inferred; "
                          "cli when nothing signals one)")
    new.add_argument("--framework", choices=sorted({f for fs in FRAMEWORKS.values() for f in fs}
                                                   - {"argparse"}),
                     help="override the framework: streamlit or fastapi (web), aiogram "
                          "(telegram), fastapi (api)")
    new.add_argument("-f", "--force", action="store_true", help="replace an existing folder")
    new.add_argument("--dry-run", action="store_true", help="plan only; print the spec")
    new.add_argument("-q", "--quiet", action="store_true", help="no progress lines")

    lst = sub.add_parser("list", help="list generated sub-agents")
    lst.add_argument("-o", "--out", type=Path, help="folder to list (default: subagents/)")

    show = sub.add_parser("show", help="show a sub-agent's spec and re-check its files")
    show.add_argument("slug", help="sub-agent folder name, or a path to it")
    show.add_argument("-o", "--out", type=Path, help="parent folder (default: subagents/)")
    show.add_argument("--json", action="store_true", help="print spec.json as-is")

    deploy = sub.add_parser("deploy", help="prepare a hosting target and print its commands",
                            description="Refresh a sub-agent's deploy folder from its files, "
                                        "re-run the gate, and print the exact commands. It "
                                        "never pushes and never holds a token; for docker it "
                                        "runs `docker build` when Docker is installed.")
    deploy.add_argument("slug", help="sub-agent folder name, or a path to it")
    deploy.add_argument("-t", "--target", required=True,
                        choices=sorted({t for ts in DEPLOY_TARGETS.values() for t in ts}),
                        help="hosting target to prepare")
    deploy.add_argument("-o", "--out", type=Path, help="parent folder (default: subagents/)")
    deploy.add_argument("--no-build", action="store_true",
                        help="docker: print the commands without running docker build")

    sub.add_parser("capabilities", help="list the capability packs installed and proposable",
                   description="List the capability packs the factory ships, the ones the "
                               "local catalogue could propose, and the ones the licence "
                               "allowlist rejects. Offline.")

    evo = sub.add_parser("evolve", help="detect a missing capability and propose an upgrade",
                         description="Check a task against the capability registry before "
                                     "generating anything. On a gap, print an additive "
                                     "upgrade from the local catalogue and ask [y/N]. It "
                                     "never writes a core file, never applies without an "
                                     "interactive yes, and declines when there is no "
                                     "terminal or CI=true. Offline: no network, no model.")
    evo.add_argument("text", nargs="*", help="the task to check (quote it)")
    evo.add_argument("--yes", action="store_true",
                     help="approve without a prompt (scripted use); still declined when "
                          "CI=true")
    evo.add_argument("--log", action="store_true", help="print the evolution ledger and exit")

    bot = sub.add_parser("bot", help="run the factory as a Telegram bot (optional extra)",
                         description="Serve the factory in Telegram: one line in, a validated "
                                     "sub-agent back as a ZIP, with the six stages shown in "
                                     "one message edited in place. Needs the bot extra "
                                     "(pip install -r requirements-bot.txt), "
                                     "TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS. Serves "
                                     "GET /healthz on $PORT for a keepalive pinger.")
    bot.add_argument("-p", "--provider", choices=("auto", *KNOWN_PROVIDERS), default="auto",
                     help="force one provider (default: auto — keyed providers, else mock)")
    bot.add_argument("--check", action="store_true",
                     help="check the bot settings offline and exit; contacts nothing")

    prov = sub.add_parser("providers", help="show the provider chain and key status")
    prov.add_argument("--check", action="store_true",
                      help="contact each keyed provider (GET /models, no tokens spent)")

    doctor = sub.add_parser("doctor", help="check python, config, providers, ruff and paths")
    doctor.add_argument("--online", action="store_true",
                        help="also contact each keyed provider to test the key")
    return parser


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


async def _cmd_new(args: argparse.Namespace, config: Config) -> int:
    """Run the factory once."""
    spec: SubAgentSpec | None = None
    if args.from_spec:
        spec = load_spec(args.from_spec)
    command = " ".join(args.text).strip()
    if not command and spec is None:
        err("describe the sub-agent to build, e.g.\n  " + EXAMPLE)
        return 2
    chain = build_chain(config, args.provider)
    factory = Factory(config, chain)
    try:
        result = await factory.generate(command, output_dir=args.out, force=args.force,
                                        dry_run=args.dry_run, spec=spec,
                                        interface=args.interface, framework=args.framework)
    finally:
        await factory.aclose()
    if result.dry_run:
        out(result.spec.to_json().rstrip())
        out(f"(dry run: nothing written; would write {result.target_dir})")
    else:
        logger.info("done in %.1fs", result.elapsed)
    # A capability the factory lacks gets the same proposal and gate as `evolve`.
    try:
        await evolve(result.spec.command or command, spec=result.spec,
                     paths=evolution_paths(), report_covered=False)
    except EvolutionError as exc:
        err(f"capability check skipped: {exc}")
    return 0


def evolution_paths() -> EvolutionPaths:
    """Evolution files of this checkout (``FATHER_EVOLUTION_ROOT`` overrides, for tests)."""
    root = os.environ.get("FATHER_EVOLUTION_ROOT")
    return EvolutionPaths(Path(root)) if root else EvolutionPaths()


def _cmd_capabilities(args: argparse.Namespace, config: Config) -> int:
    """List installed, proposable and rejected capability packs."""
    for line in capability_lines(evolution_paths()):
        out(line)
    return 0


async def _cmd_evolve(args: argparse.Namespace, config: Config) -> int:
    """Detect a capability gap for a task and propose an upgrade; never generates."""
    paths = evolution_paths()
    if args.log:
        for line in ledger_lines(paths):
            out(line)
        return 0
    task = " ".join(args.text).strip()
    if not task:
        err('describe the task to check, e.g.\n  python main.py evolve "watch a kafka topic '
            'and alert on spikes"')
        return 2
    return await evolve(task, spec=planned_spec(task), paths=paths, assume_yes=args.yes)


def _subagent_dirs(root: Path) -> list[Path]:
    """Folders under ``root`` that contain a spec.json."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "spec.json").is_file())


BOT_EXTRA_HINT = ("the Telegram bot needs the optional bot extra, which is not installed:\n"
                  "  pip install -r requirements-bot.txt")


def _cmd_bot(args: argparse.Namespace, config: Config) -> int:
    """Run the Telegram bot; aiogram is imported only here, so the core never needs it."""
    try:
        from . import bot
    except ImportError as exc:
        err(f"{BOT_EXTRA_HINT}\n  ({exc})")
        return 2
    if args.check:
        settings = bot.BotSettings.load(config)
        out("telegram bot settings (nothing was contacted)")
        for line in settings.describe():
            out(f"  {line}")
        return 0
    return bot.run(config, provider=args.provider)


def _cmd_list(args: argparse.Namespace, config: Config) -> int:
    """List sub-agents in the output folder."""
    root = args.out or config.output_dir
    dirs = _subagent_dirs(root)
    if not dirs:
        out(f"no sub-agents in {root} yet. Try:\n  {EXAMPLE}")
        return 0
    out(f"{'slug':<28}{'domain':<13}{'interface':<20}{'libraries':<40}planned by")
    for path in dirs:
        try:
            spec = load_spec(path)
        except FatherAgentError as exc:
            out(f"{path.name:<28}{'?':<13}{'?':<20}"
                f"{'(unreadable spec: ' + str(exc)[:30] + ')':<40}")
            continue
        libs = ", ".join(d.package for d in spec.dependencies) or "stdlib"
        out(f"{spec.slug:<28}{spec.domain:<13}{spec.delivery.label:<20}{libs[:38]:<40}"
            f"{spec.planned_by or '?'}")
    return 0


#: Folders never read back from a sub-agent (history, caches, secrets).
_SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv", ".git"}


def read_bundle(folder: Path) -> dict[str, str]:
    """Every text file of a written sub-agent, ``relative/posix/path -> text``.

    ``.env`` (your secrets), ``*_data/`` history and caches are skipped.
    """
    files: dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if not path.is_file() or path.name == ".env" or path.suffix in (".pyc", ".png") \
                or any(p in _SKIP_PARTS or p.endswith("_data") for p in rel.parts[:-1]):
            continue
        try:
            files[rel.as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def _resolve_folder(slug: str, out_dir: Path | None, config: Config) -> Path:
    """A sub-agent folder from a slug or a path."""
    candidate = Path(slug)
    return candidate if candidate.is_dir() else (out_dir or config.output_dir) / slug


def _cmd_show(args: argparse.Namespace, config: Config) -> int:
    """Print a sub-agent's spec and re-run the (static) validator on its files."""
    folder = _resolve_folder(args.slug, args.out, config)
    spec = load_spec(folder)
    if args.json:
        out(spec.to_json().rstrip())
        return 0
    out(f"{spec.name}  ({folder})")
    out(f"  {spec.summary}")
    out(f"  command      {spec.command}")
    out(f"  domain       {spec.domain}")
    out(f"  interface    {spec.delivery.label}"
        + ("" if spec.has_delivery_block else "  (spec predates the delivery layer)"))
    out(f"  deploy       {', '.join(spec.delivery.deploy)}")
    out(f"  libraries    {', '.join(d.package for d in spec.dependencies) or 'stdlib only'}")
    out(f"  env vars     {', '.join(e.name for e in spec.env_vars) or 'none'}")
    out(f"  classes      {', '.join(c.name for c in spec.classes)}")
    if spec.schedule_seconds:
        out(f"  schedule     every {spec.schedule_seconds}s")
    out(f"  planned by   {spec.planned_by or '?'}")
    out(f"  run          {spec.run_example}")
    legacy = not spec.has_delivery_block
    expected = list(LEGACY_FILES) if legacy else expected_files(spec, BUNDLE_FILES)
    files = read_bundle(folder)
    missing = []
    for name in expected:
        path = folder / name
        present = path.is_file()
        if not present:
            missing.append(name)
        out(f"  {name:<34}{f'{path.stat().st_size} bytes' if present else 'MISSING'}")
    if legacy:
        files = {n: t for n, t in files.items() if n in LEGACY_FILES and n.endswith(".py")}
    reports = asyncio.run(Validator().validate_bundle(files, spec)) if files else []
    for report in reports:
        out(f"  check {report.filename:<15}{report.summary}")
        for problem in report.problems:
            out(f"      - {problem}")
    if reports:
        out(f"  gate         {gate_summary(reports)}")
    return 0 if all(r.ok for r in reports) and not missing else 1


def _cmd_deploy(args: argparse.Namespace, config: Config) -> int:
    """Refresh one hosting target's folder, re-run the gate and print its commands."""
    folder = _resolve_folder(args.slug, args.out, config)
    spec = load_spec(folder)
    if not spec.has_delivery_block:
        err(f"{folder} predates the delivery layer; rebuild it first:\n  python main.py new "
            f"--from-spec {folder / 'spec.json'} --force")
        return 2
    if args.target not in spec.delivery.deploy:
        err(f"{spec.slug} ({spec.delivery.label}) is prepared for "
            f"{', '.join(spec.delivery.deploy)}, not {args.target}")
        return 2
    files = read_bundle(folder)
    missing = [n for n in deploy_sources(spec) if n not in files]
    if missing:
        err(f"{folder} is missing {', '.join(missing)}")
        return 1
    for target in spec.delivery.deploy:
        if target not in TARGET_FOLDERS:
            continue
        refreshed = render_target(spec, target, files)
        for rel, text in refreshed.items():
            # Copies always follow the root files, in every target folder so the
            # gate's copy check holds; a manifest you edited is kept.
            if rel.rsplit("/", 1)[-1] in deploy_sources(spec) or rel not in files:
                path = folder / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                files[rel] = text
        if target == args.target:
            out(f"prepared {folder / TARGET_FOLDERS[target]}/ "
                f"({', '.join(sorted(Path(r).name for r in refreshed))})")
    reports = asyncio.run(Validator().validate_bundle(files, spec))
    out(f"gate: {gate_summary(reports)} · nothing was executed")
    failed = [p for r in reports for p in r.problems]
    if failed:
        err("the bundle failed the gate; fix it before deploying:\n  - " + "\n  - ".join(failed))
        return 1
    commands = target_commands(spec, args.target, str(folder))
    if args.target == "docker" and not args.no_build and shutil.which("docker"):
        image = spec.slug.replace("_", "-")
        out(f"running: docker build -t {image} {folder}")
        code = subprocess.run(["docker", "build", "-t", image, str(folder)], check=False).returncode
        if code != 0:
            err(f"docker build exited {code}")
            return 1
        commands = commands[2:]
    elif args.target == "docker" and not args.no_build:
        out("docker is not installed here, so nothing was built; run these where it is:")
    out(f"next ({args.target}):")
    for line in commands:
        out(f"  {line}")
    return 0


def _chain_rows(config: Config) -> list[tuple[str, str, str, str]]:
    """``(name, key status, model, endpoint)`` for each known provider."""
    return [
        ("groq", mask(config.groq_api_key), config.groq_model, config.groq_base_url),
        ("huggingface", mask(config.hf_token), config.hf_model, config.hf_base_url),
        ("local", "on" if config.local_llm_url else "off (opt-in)", config.local_llm_model,
         config.local_llm_url or "set FATHER_LOCAL_LLM_URL"),
        ("mock", "always available", "offline-templates", "in-process, no network"),
    ]


async def _cmd_providers(args: argparse.Namespace, config: Config) -> int:
    """Show the provider chain and optionally check the keys."""
    out(f"provider order: {' → '.join(config.provider_order)}"
        f"{' → mock' if config.allow_mock_fallback else ''}")
    for name, key, model, endpoint in _chain_rows(config):
        out(f"  {name:<12}{key:<20}{model:<34}{endpoint}")
    chain = build_chain(config, warn=False)
    out(f"active chain: {' → '.join(p.label for p in chain.providers)}")
    if args.check:
        try:
            for label, status in (await chain.check_all()).items():
                out(f"  check {label:<44}{status}")
        finally:
            await chain.aclose()
    else:
        await chain.aclose()
    return 0


def _version(dist: str) -> str:
    """Installed version of a distribution, or ``missing``."""
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


async def _cmd_doctor(args: argparse.Namespace, config: Config | None,
                      config_error: str = "") -> int:
    """Report environment, configuration and provider status."""
    failures: list[str] = []

    def row(name: str, value: str, ok: bool | None = True) -> None:
        mark = {True: "ok", False: "FAIL", None: "--"}[ok]
        out(f"  {name:<13}{mark:<6}{value}")
        if ok is False:
            failures.append(name)

    out(f"Father Agent {__version__} — doctor")
    py = sys.version_info
    row("python", f"{py.major}.{py.minor}.{py.micro} ({sys.executable})", py >= (3, 11))
    if config is None:
        row("config", config_error, False)
        out(f"result: {len(failures)} problem(s): {', '.join(failures)}")
        return 1
    env_note = str(config.env_file) if config.env_file else \
        f"no .env (using environment + defaults; copy .env.example to {PROJECT_ROOT / '.env'})"
    row("config", env_note, True if config.env_file else None)
    for name, key, model, endpoint in _chain_rows(config)[:3]:
        keyed = config.has_key(name)
        row(name, f"{key} · {model} · {endpoint}", True if keyed else None)
    chain = build_chain(config, warn=False)
    mode = ("offline mock (no keys: deterministic templates, no network)"
            if chain.is_mock_only else "free remote models")
    row("chain", f"{' → '.join(p.label for p in chain.providers)} · {mode}")
    row("free-only", "endpoints restricted to " + ", ".join(sorted(FREE_HOSTS)))
    if args.online:
        if chain.is_mock_only:
            row("online", "skipped: no keys configured", None)
        else:
            for label, status in (await chain.check_all()).items():
                row("online", f"{label}: {status}", status.startswith(("key ok", "reachable")))
    await chain.aclose()
    try:
        prompts = PromptLibrary(config.prompts_dir)
        row("prompts", " · ".join(prompts.versions.values()))
    except ConfigError as exc:
        row("prompts", str(exc), False)
    ruff = find_ruff()
    row("ruff", f"{ruff[0]} {_version('ruff')}" if ruff else
        "not installed: lint step will be skipped (pip install ruff)", True if ruff else None)
    docker = shutil.which("docker")
    if importlib.util.find_spec("aiogram") is None:
        row("telegram", "bot extra not installed (optional: pip install -r "
                        "requirements-bot.txt)", None)
    else:
        token = "token " + mask(config.telegram_bot_token)
        row("telegram", f"aiogram {_version('aiogram')} · {token} · python main.py bot",
            True if config.telegram_bot_token else None)
    row("docker", docker or "not installed: `deploy --target docker` prints the commands "
                            "instead of building", True if docker else None)
    required = ("httpx", "pydantic", "python-dotenv", "pyyaml")
    versions = {d: _version(d) for d in (*required, "smolagents")}
    row("packages", " · ".join(f"{d} {v}" for d, v in versions.items()),
        all(versions[d] != "missing" for d in required))
    out_dir = config.output_dir
    writable = _writable(out_dir)
    row("output dir", f"{out_dir} ({'writable' if writable else 'NOT writable'})", writable)
    row("log file", str(config.log_dir / LOG_FILE_NAME), _writable(config.log_dir))
    if failures:
        out(f"result: {len(failures)} problem(s): {', '.join(failures)}")
        return 1
    if chain.is_mock_only:
        out("result: ready in offline mock mode. Add GROQ_API_KEY or HF_TOKEN to .env "
            "(see .env.example) for real models.")
    else:
        out("result: ready.")
    return 0


def _writable(folder: Path) -> bool:
    """True if ``folder`` exists (or can be created) and accepts writes."""
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return os.access(probe, os.W_OK)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, configure logging and run one command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        config: Config | None = Config.load(args.env_file)
        config_error = ""
    except ConfigError as exc:
        config, config_error = None, str(exc)

    if config is None:
        if args.command == "doctor":
            return asyncio.run(_cmd_doctor(args, None, config_error))
        err(f"configuration error: {config_error}")
        return 2

    setup_logging(config, verbose=args.verbose, quiet=getattr(args, "quiet", False))
    logger.debug("argv=%s", json.dumps(argv if argv is not None else sys.argv[1:]))
    try:
        if args.command in ("new", "generate"):
            return asyncio.run(_cmd_new(args, config))
        if args.command == "list":
            return _cmd_list(args, config)
        if args.command == "show":
            return _cmd_show(args, config)
        if args.command == "deploy":
            return _cmd_deploy(args, config)
        if args.command == "capabilities":
            return _cmd_capabilities(args, config)
        if args.command == "evolve":
            return asyncio.run(_cmd_evolve(args, config))
        if args.command == "bot":
            return _cmd_bot(args, config)
        if args.command == "providers":
            return asyncio.run(_cmd_providers(args, config))
        if args.command == "doctor":
            return asyncio.run(_cmd_doctor(args, config))
    except ConfigError as exc:
        err(f"configuration error: {exc}")
        return 2
    except FatherAgentError as exc:
        logger.error("%s", exc, extra={"console": False})
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    parser.error(f"unknown command {args.command!r}")
    return 2
