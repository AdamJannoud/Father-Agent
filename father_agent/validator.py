"""The validator gate.

Every generated file passes through here while it is still a string. Nothing
is written to the output folder unless every check passes, and nothing that
was generated is ever imported or executed. The checks, in order:

1. ``ast``      — the source parses.
2. ``compile``  — ``py_compile`` byte-compiles it (in a throw-away temp dir).
3. ``ruff``     — lint for real errors (syntax, undefined names, unused
                  imports, bugbear), when ruff is installed.
4. ``imports``  — every import is standard library, a declared dependency,
                  or another file of the same sub-agent. Resolved statically
                  from the AST: the module is never imported.
5. ``consistency`` — the classes and methods the spec promised exist, the
                  entry point exists, and the tests import only names that
                  agent.py defines.
6. ``policy``   — the standards: docstrings, async, logging not print,
                  try/except, and no eval/exec/os.system/shell=True or paid
                  API SDKs.

Checks 1-6 run on every Python file. A delivery bundle (one that carries
requirements.txt, a Dockerfile and the rest of the kit) gets three more,
which read text and never import or run anything either:

7. ``bundle``   — requirements.txt covers every third-party import in the
                  bundle, and .env.example lists every declared variable and
                  every one the code reads.
8. ``deploy``   — the Dockerfile's CMD names a file that exists; each host
                  manifest (a Space README, render.yaml) parses and points at
                  files that exist; each deploy folder's copies match the root.
9. ``secrets``  — no literal token, API key, private key or long hex string
                  anywhere in the bundle, and .env.example holds no values.

A capability pack proposed by the self-evolution module gets checks 1-3 on
its templates and one more on its dependencies:

10. ``licence`` — every dependency carries a licence on the open-source
                  allowlist (:data:`LICENCE_ALLOWLIST`), is not a paid API SDK,
                  and the pack needs no account or token. A model that ignores
                  an instruction cannot get past this: it is code, not a prompt.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import py_compile
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .delivery import (
    HOST_SET,
    TARGET_FOLDERS,
    deploy_sources,
    env_example_entries,
    env_names_read,
    interface_file,
    requirement_name,
)
from .spec import PAID_SDKS, SubAgentSpec, import_name_for

logger = logging.getLogger(__name__)

RUFF_RULES = "E9,F,B"
MAX_SOURCE_BYTES = 200_000
#: Modules every generated test file may import besides the spec's dependencies.
TEST_ALLOWED = frozenset({"pytest", "_pytest"})
_BANNED_CALLS = {"eval", "exec", "__import__", "compile"}
_BANNED_ATTR_CALLS = {("os", "system"), ("os", "popen"), ("pickle", "loads")}
#: Order of the checks in the one-line gate summary.
CHECK_ORDER = ("ast", "compile", "ruff", "imports", "consistency", "policy",
               "bundle", "deploy", "secrets", "licence")
#: Open-source licences (SPDX ids) a capability-pack dependency may carry.
LICENCE_ALLOWLIST = frozenset({
    "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "PSF-2.0", "MPL-2.0",
    "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0", "LGPL-3.0-only",
    "LGPL-3.0-or-later",
})
#: Name of the report that holds the bundle-level checks.
BUNDLE_REPORT = "bundle"
_SECRET_PATTERNS = (
    ("a Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("a Groq API key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("a Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("an sk- API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a long hex string", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("a base58 private key", re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{80,90}\b")),
)
_CMD_FILE_RE = re.compile(r"(?<![\w/.-])([\w./-]+\.py)\b")
_CMD_MODULE_RE = re.compile(r"(?<![\w.])(\w+):app\b")


def file_role(filename: str) -> str:
    """``agent``, ``test``, ``interface`` (app.py/bot.py) or ``support`` (anything else)."""
    name = Path(filename).name
    if name.startswith("test_"):
        return "test"
    if name == "agent.py":
        return "agent"
    if name in ("app.py", "bot.py") and "/" not in filename:
        return "interface"
    return "support"


def gate_summary(reports: list[FileReport]) -> str:
    """One label per check across every report: ``ast ok · ... · secrets ok``."""
    labels: list[str] = []
    for name in CHECK_ORDER:
        checks = [c for r in reports for c in r.checks if c.name == name]
        if not checks:
            continue
        if not all(c.ok for c in checks):
            labels.append(f"{name} FAILED")
        elif all(c.skipped for c in checks):
            labels.append(f"{name} skipped")
        else:
            labels.append(f"{name} ok")
    return " · ".join(labels)


@dataclass
class CheckResult:
    """The outcome of one check on one file."""

    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    @property
    def label(self) -> str:
        """``ruff ok`` / ``ruff skipped`` / ``ruff FAILED``."""
        if self.skipped:
            return f"{self.name} skipped"
        return f"{self.name} {'ok' if self.ok else 'FAILED'}"


@dataclass
class FileReport:
    """All check results for one generated file."""

    filename: str
    role: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no check failed (skipped checks do not fail)."""
        return all(c.ok for c in self.checks)

    @property
    def problems(self) -> list[str]:
        """Human-readable failures, one per line of detail."""
        out: list[str] = []
        for check in self.checks:
            if not check.ok:
                for line in (check.detail or "failed").splitlines():
                    if line.strip():
                        out.append(f"{check.name}: {line.strip()}")
        return out

    @property
    def summary(self) -> str:
        """``ast ok · compile ok · ruff ok · imports ok ...``."""
        return " · ".join(c.label for c in self.checks)


def licence_allowed(licence: str) -> bool:
    """True when ``licence`` (an SPDX id, any case) is on :data:`LICENCE_ALLOWLIST`."""
    wanted = licence.strip().lower()
    return any(wanted == allowed.lower() for allowed in LICENCE_ALLOWLIST)


def check_licences(dependencies: list[tuple[str, str]], *,
                   needs_account: bool = False) -> CheckResult:
    """Check 10: ``(package, licence)`` pairs against the allowlist and the paid SDKs."""
    problems: list[str] = []
    for package, licence in dependencies:
        if import_name_for(package) in PAID_SDKS or package.lower() in PAID_SDKS:
            problems.append(f"{package} is a paid API SDK")
        if not licence.strip():
            problems.append(f"{package} declares no licence")
        elif not licence_allowed(licence):
            problems.append(f"{package} is licensed {licence}, which is not on the "
                            f"open-source allowlist")
    if needs_account:
        problems.append("the pack needs an account or a token; only free, keyless "
                        "components may be proposed")
    return CheckResult("licence", not problems, "\n".join(problems))


def find_ruff() -> list[str] | None:
    """Locate the ruff executable (on PATH or next to this interpreter)."""
    found = shutil.which("ruff")
    if found:
        return [found]
    sibling = Path(sys.executable).parent / ("ruff.exe" if sys.platform == "win32" else "ruff")
    if sibling.is_file():
        return [str(sibling)]
    return None


def _module_level_names(tree: ast.Module) -> set[str]:
    """Names bound at module level: defs, classes, assignments and imports."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


class Validator:
    """Runs the gate over generated sources. Never executes them."""

    def __init__(self, *, ruff_command: list[str] | None = None, use_ruff: bool = True,
                 require_ruff: bool = False, ruff_rules: str = RUFF_RULES) -> None:
        """Configure the gate.

        Args:
            ruff_command: Explicit ruff command; auto-detected when None.
            use_ruff: Set False to skip the ruff step entirely.
            require_ruff: Fail (rather than skip) when ruff is not installed.
            ruff_rules: Rule selection passed to ``ruff check --select``.
        """
        self.ruff_command = (ruff_command or find_ruff()) if use_ruff else None
        self.require_ruff = require_ruff
        self.ruff_rules = ruff_rules

    # ------------------------------------------------------------------ API

    async def validate_bundle(self, files: dict[str, str],
                              spec: SubAgentSpec) -> list[FileReport]:
        """Validate every file of a sub-agent concurrently.

        Python files get checks 1-6. Copies under ``deploy/`` are not linted
        again: the ``deploy`` check proves they match the root files. When the
        bundle carries anything besides Python (the delivery kit), one more
        report named ``bundle`` holds the bundle, deploy and secrets checks.
        """
        python = {name: src for name, src in files.items()
                  if name.endswith(".py") and not name.startswith("deploy/")}
        reports = list(await asyncio.gather(
            *(self.validate_file(name, src, spec=spec, bundle=python)
              for name, src in python.items())
        ))
        if any(not name.endswith(".py") for name in files):
            reports.append(self.validate_delivery(files, spec))
        return reports

    async def validate_file(self, filename: str, source: str, *, spec: SubAgentSpec,
                            bundle: dict[str, str] | None = None) -> FileReport:
        """Run every check on one file and return the report."""
        role = file_role(filename)
        report = FileReport(filename, role)
        bundle = bundle or {filename: source}

        tree = self._check_ast(filename, source, report)
        if tree is None:
            return report
        report.checks.append(await asyncio.to_thread(self._check_compile, filename, source))
        report.checks.append(await self._check_ruff(filename, source))
        report.checks.append(self._check_imports(tree, role, spec, bundle))
        report.checks.append(self._check_consistency(tree, role, spec, bundle, source))
        report.checks.append(self._check_policy(tree, role, spec))
        logger.info("validated %s: %s", filename, report.summary)
        return report

    def validate_delivery(self, files: dict[str, str], spec: SubAgentSpec) -> FileReport:
        """Checks 7-9 over the whole bundle (code, kit, deploy folders, docs)."""
        report = FileReport(BUNDLE_REPORT, "bundle")
        report.checks.append(self._check_bundle(files, spec))
        report.checks.append(self._check_deploy(files, spec))
        report.checks.append(self._check_secrets(files))
        logger.info("validated the bundle: %s", report.summary)
        return report

    async def validate_pack(self, files: dict[str, str], dependencies: list[tuple[str, str]],
                            *, needs_account: bool = False) -> list[FileReport]:
        """Checks 1-3 on each Python file of a capability pack, then check 10.

        The last report, named ``licence``, holds the licence check.
        """
        reports: list[FileReport] = []
        for name, source in files.items():
            if not name.endswith(".py"):
                continue
            report = FileReport(name, "support")
            if self._check_ast(name, source, report) is not None:
                report.checks.append(await asyncio.to_thread(self._check_compile, name, source))
                report.checks.append(await self._check_ruff(name, source))
            reports.append(report)
        licence = FileReport("licence", "pack")
        licence.checks.append(check_licences(dependencies, needs_account=needs_account))
        reports.append(licence)
        return reports

    # --------------------------------------------------------------- checks

    def _check_ast(self, filename: str, source: str, report: FileReport) -> ast.Module | None:
        """Parse the source; record the result and return the tree or None."""
        if len(source.encode("utf-8", "replace")) > MAX_SOURCE_BYTES:
            report.checks.append(CheckResult("ast", False, f"file is larger than "
                                                           f"{MAX_SOURCE_BYTES} bytes"))
            return None
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            report.checks.append(CheckResult(
                "ast", False, f"line {exc.lineno}: {exc.msg}"))
            return None
        except ValueError as exc:  # e.g. null bytes
            report.checks.append(CheckResult("ast", False, str(exc)))
            return None
        report.checks.append(CheckResult("ast", True))
        return tree

    def _check_compile(self, filename: str, source: str) -> CheckResult:
        """Byte-compile with py_compile inside a temporary directory."""
        try:
            with tempfile.TemporaryDirectory(prefix="father-validate-") as tmp:
                path = Path(tmp) / Path(filename).name
                path.write_text(source, encoding="utf-8")
                py_compile.compile(str(path), cfile=str(path) + "c", doraise=True)
        except py_compile.PyCompileError as exc:
            return CheckResult("compile", False, exc.msg.strip().splitlines()[-1])
        except OSError as exc:
            return CheckResult("compile", False, f"could not stage file: {exc}")
        return CheckResult("compile", True)

    async def _check_ruff(self, filename: str, source: str) -> CheckResult:
        """Lint with ruff over stdin; skipped (or failed) when ruff is absent."""
        if not self.ruff_command:
            if self.require_ruff:
                return CheckResult("ruff", False, "ruff is required but not installed")
            return CheckResult("ruff", True, "ruff not installed", skipped=True)
        cmd = [*self.ruff_command, "check", "--isolated", "--no-cache", "--quiet",
               "--select", self.ruff_rules, "--output-format", "concise",
               "--stdin-filename", Path(filename).name, "-"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(source.encode()), timeout=60)
        except (OSError, TimeoutError) as exc:
            logger.warning("ruff could not run: %s", exc)
            if self.require_ruff:
                return CheckResult("ruff", False, f"ruff could not run: {exc}")
            return CheckResult("ruff", True, f"ruff could not run: {exc}", skipped=True)
        if proc.returncode == 0:
            return CheckResult("ruff", True)
        text = (out.decode(errors="replace") or err.decode(errors="replace")).strip()
        lines = [ln.replace("-:", f"{filename}:", 1) for ln in text.splitlines()
                 if ln.strip() and not ln.startswith(("Found ", "[*]", "No fixes"))]
        if proc.returncode == 1:
            return CheckResult("ruff", False, "\n".join(lines[:20]) or "lint errors")
        logger.warning("ruff exited %s: %s", proc.returncode, text[:300])
        return CheckResult("ruff", False, f"ruff error: {text[:300]}")

    def _check_imports(self, tree: ast.Module, role: str, spec: SubAgentSpec,
                       bundle: dict[str, str]) -> CheckResult:
        """Every import must be stdlib, a declared dependency, or a bundle module."""
        local = {Path(name).stem for name in bundle}
        allowed = set(sys.stdlib_module_names) | spec.import_names | local | {"__future__"}
        if role == "test":
            allowed |= TEST_ALLOWED
        problems: list[str] = []
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    target = (node.module or "").split(".")[0]
                    names = [target] if target else [a.name for a in node.names]
                    problems += [f"line {node.lineno}: relative import of {n!r} is not part "
                                 f"of this sub-agent" for n in names if n not in local]
                    continue
                modules = [node.module or ""]
            for module in modules:
                top = module.split(".")[0]
                if module in PAID_SDKS or top in PAID_SDKS:
                    problems.append(f"line {node.lineno}: {module!r} is a paid-API SDK")
                elif top not in allowed:
                    problems.append(
                        f"line {node.lineno}: imports {top!r}, which is neither standard "
                        f"library nor a declared dependency (add it to the spec or remove it)")
        return CheckResult("imports", not problems, "\n".join(problems))

    def _check_consistency(self, tree: ast.Module, role: str, spec: SubAgentSpec,
                           bundle: dict[str, str], source: str = "") -> CheckResult:
        """Check promised classes/methods/entry point, and imports between bundle files."""
        problems: list[str] = []
        if role == "agent":
            classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
            for cls in spec.classes:
                node = classes.get(cls.name)
                if node is None:
                    problems.append(f"class {cls.name} from the spec is not defined")
                    continue
                methods = {n.name for n in node.body
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                missing = [m for m in cls.methods if m not in methods]
                if missing:
                    problems.append(f"class {cls.name} is missing method(s): "
                                    f"{', '.join(missing)}")
            functions = {n.name for n in tree.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            if spec.entrypoint not in functions:
                problems.append(f"entry point function {spec.entrypoint}() is not defined")
            return CheckResult("consistency", not problems, "\n".join(problems))

        problems += self._local_import_problems(tree, bundle)
        if role == "test":
            if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name.startswith("test") for n in ast.walk(tree)):
                problems.append("no test functions found")
        elif role == "interface":
            problems += self._interface_problems(tree, spec, source)
        return CheckResult("consistency", not problems, "\n".join(problems))

    @staticmethod
    def _local_import_problems(tree: ast.Module, bundle: dict[str, str]) -> list[str]:
        """``from agent import X``: X must be defined by agent.py (any bundle module)."""
        modules = {Path(name).stem: src for name, src in bundle.items() if name.endswith(".py")}
        problems: list[str] = []
        defined: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module not in modules:
                continue
            if node.module not in defined:
                try:
                    defined[node.module] = _module_level_names(ast.parse(modules[node.module]))
                except (SyntaxError, ValueError):
                    defined[node.module] = set()
            for alias in node.names:
                if alias.name != "*" and alias.name not in defined[node.module]:
                    problems.append(f"line {node.lineno}: imports {alias.name!r} from "
                                    f"{node.module}, which {node.module}.py does not define")
        return problems

    @staticmethod
    def _interface_problems(tree: ast.Module, spec: SubAgentSpec, source: str) -> list[str]:
        """What app.py / bot.py must provide for its interface and framework."""
        problems: list[str] = []
        imported = {(a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[0]
                    for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in n.names}
        names = _module_level_names(tree)
        delivery = spec.delivery
        if "agent" not in imported:
            problems.append("does not import the agent core (from agent import ...)")
        if delivery.framework not in imported:
            problems.append(f"does not import {delivery.framework}, the spec's framework")
        if delivery.framework == "fastapi":
            if "app" not in names:
                problems.append("defines no module-level FastAPI `app`")
            for route in ("/health", "/run") if delivery.interface == "api" else ("/health",):
                if f'"{route}"' not in source and f"'{route}'" not in source:
                    problems.append(f"has no {route} route")
        if delivery.framework == "aiogram":
            for name in ("main", "BOT_COMMANDS", "WEBHOOK_PATH"):
                if name not in names:
                    problems.append(f"defines no module-level {name} "
                                    f"(scripts/setup_bot.py and the host need it)")
            if "--dry-run" not in source:
                problems.append("has no --dry-run flag (prove the handlers without a token)")
        return problems

    def _check_policy(self, tree: ast.Module, role: str,
                      spec: SubAgentSpec | None = None) -> CheckResult:
        """Enforce the code standards and refuse dangerous calls."""
        problems: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BANNED_CALLS:
                problems.append(f"line {node.lineno}: {func.id}() is not allowed")
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if (func.value.id, func.attr) in _BANNED_ATTR_CALLS:
                    problems.append(f"line {node.lineno}: {func.value.id}.{func.attr}() "
                                    f"is not allowed")
                if func.value.id == "subprocess" and any(
                        k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value
                        for k in node.keywords):
                    problems.append(f"line {node.lineno}: subprocess with shell=True "
                                    f"is not allowed")
            if role != "test" and isinstance(func, ast.Name) and func.id == "print":
                problems.append(f"line {node.lineno}: use logging instead of print()")

        if not ast.get_docstring(tree):
            problems.append("module docstring is missing")
        for node in tree.body:
            targets = [node]
            if isinstance(node, ast.ClassDef):
                targets += [n for n in node.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for item in targets:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                public = not item.name.startswith("_") or item.name == "__init__"
                if role != "test" and public and not ast.get_docstring(item):
                    problems.append(f"line {item.lineno}: {item.name} has no docstring")

        nodes = list(ast.walk(tree))
        framework = spec.delivery.framework if spec else ""
        needs_async = role == "agent" or (role == "interface" and framework != "streamlit")
        if needs_async and not any(isinstance(n, ast.AsyncFunctionDef) for n in nodes):
            problems.append("no async def: I/O must use asyncio")
        if role in ("agent", "interface") and not any(isinstance(n, ast.Try) for n in nodes):
            problems.append("no try/except: external calls must handle errors")
        if role != "test" and not any(
                isinstance(n, ast.Import) and any(a.name == "logging" for a in n.names)
                for n in nodes):
            problems.append("logging is not imported")
        return CheckResult("policy", not problems, "\n".join(problems))

    # ------------------------------------------------------ bundle checks

    def _check_bundle(self, files: dict[str, str], spec: SubAgentSpec) -> CheckResult:
        """requirements.txt covers every import; .env.example covers every variable."""
        problems: list[str] = []
        code = {name: src for name, src in files.items()
                if name.endswith(".py") and not name.startswith("deploy/")}
        local = {Path(name).stem for name in code}
        requirements = files.get("requirements.txt")
        if requirements is None:
            problems.append("requirements.txt is missing")
        else:
            by_package = {d.package.split("[")[0].strip().lower(): d.import_name
                          for d in spec.dependencies}
            covered = set()
            for line in requirements.splitlines():
                name = requirement_name(line)
                if name:
                    covered |= {import_name_for(name), by_package.get(name, "")}
            stdlib = set(sys.stdlib_module_names) | {"__future__"}
            for name, src in sorted(code.items()):
                if file_role(name) == "test":
                    continue
                for top in sorted(_third_party_imports(src, stdlib | local)):
                    if top not in covered:
                        problems.append(f"{name} imports {top!r}, which requirements.txt "
                                        f"does not list")
        env_text = files.get(".env.example")
        if env_text is None:
            problems.append(".env.example is missing")
        else:
            listed = set(env_example_entries(env_text))
            for env in spec.env_vars:
                if env.name not in listed:
                    problems.append(f".env.example does not list declared variable {env.name}")
            for name, src in sorted(code.items()):
                if file_role(name) == "test":
                    continue
                for var in sorted(env_names_read(src) - listed - HOST_SET):
                    problems.append(f"{name} reads {var}, which .env.example does not list")
        return CheckResult("bundle", not problems, "\n".join(problems))

    def _check_deploy(self, files: dict[str, str], spec: SubAgentSpec) -> CheckResult:
        """Dockerfiles run files that exist; manifests parse; deploy copies match the root."""
        problems: list[str] = []
        problems += _dockerfile_problems(files, "", "Dockerfile")
        iface = interface_file(spec)
        if iface and iface not in files:
            problems.append(f"{iface} is missing for interface {spec.delivery.interface}")
        if spec.delivery.interface == "telegram" and "scripts/setup_bot.py" not in files:
            problems.append("scripts/setup_bot.py is missing")
        for target in spec.delivery.deploy:
            folder = TARGET_FOLDERS.get(target)
            if not folder:
                continue
            for name in deploy_sources(spec):
                copy = files.get(f"{folder}/{name}")
                if copy is None:
                    problems.append(f"{folder}/{name} is missing")
                elif name in files and copy != files[name]:
                    problems.append(f"{folder}/{name} differs from the root {name} "
                                    f"(re-run: python main.py deploy {spec.slug} --target "
                                    f"{target})")
            if target == "hf-spaces":
                problems += _space_problems(files, folder)
            elif target == "render":
                problems += _render_problems(files, folder)
        return CheckResult("deploy", not problems, "\n".join(problems))

    @staticmethod
    def _check_secrets(files: dict[str, str]) -> CheckResult:
        """No credential-shaped literal anywhere; .env.example values stay empty."""
        problems: list[str] = []
        for name, text in sorted(files.items()):
            for number, line in enumerate(text.splitlines(), start=1):
                for label, pattern in _SECRET_PATTERNS:
                    if pattern.search(line):
                        problems.append(f"{name}:{number}: looks like {label}; read it from "
                                        f"the environment instead")
                        break
        for name, (_, value) in env_example_entries(files.get(".env.example", "")).items():
            if value:
                problems.append(f".env.example: {name} has a value; the example must stay "
                                f"empty")
        return CheckResult("secrets", not problems, "\n".join(problems[:20]))


def _third_party_imports(source: str, known: set[str]) -> set[str]:
    """Top-level absolute imports of ``source`` that are not in ``known``."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found - known


def _referenced_files(command: str) -> set[str]:
    """Files a start command runs: ``x.py`` arguments and ``module:app`` targets."""
    found = set(_CMD_FILE_RE.findall(command))
    found |= {f"{module}.py" for module in _CMD_MODULE_RE.findall(command)}
    return found


def _dockerfile_problems(files: dict[str, str], folder: str, name: str) -> list[str]:
    """A Dockerfile needs FROM and a CMD whose files exist next to it."""
    path = f"{folder}/{name}" if folder else name
    text = files.get(path)
    if text is None:
        return [f"{path} is missing"]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not any(ln.upper().startswith("FROM ") for ln in lines):
        return [f"{path} has no FROM line"]
    cmds = [ln[4:].strip() for ln in lines if ln.upper().startswith(("CMD ", "ENTRYPOINT "))]
    if not cmds:
        return [f"{path} has no CMD"]
    problems = []
    refs = _referenced_files(cmds[-1])
    if not refs:
        problems.append(f"{path}: CMD {cmds[-1]} does not name a Python file to run")
    for ref in sorted(refs):
        target = f"{folder}/{ref}" if folder else ref
        if target not in files:
            problems.append(f"{path}: CMD runs {ref}, which is not in the bundle")
    return problems


def _space_problems(files: dict[str, str], folder: str) -> list[str]:
    """A Space README needs valid YAML frontmatter and, for sdk docker, a Dockerfile."""
    path = f"{folder}/README.md"
    text = files.get(path, "")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return [f"{path} has no YAML frontmatter"]
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [f"{path} frontmatter is not valid YAML: {exc}"]
    if not isinstance(meta, dict):
        return [f"{path} frontmatter is not a mapping"]
    sdk = meta.get("sdk")
    if sdk not in ("docker", "gradio", "static"):
        return [f"{path}: sdk {sdk!r} is not one of docker, gradio, static"]
    if sdk == "docker":
        problems = _dockerfile_problems(files, folder, "Dockerfile")
        if not isinstance(meta.get("app_port", 7860), int):
            problems.append(f"{path}: app_port must be an integer")
        return problems
    app_file = meta.get("app_file")
    if not app_file or f"{folder}/{app_file}" not in files:
        return [f"{path}: app_file {app_file!r} is not in {folder}/"]
    return []


def _render_problems(files: dict[str, str], folder: str) -> list[str]:
    """render.yaml must parse, declare services, and start files that exist."""
    path = f"{folder}/render.yaml"
    if path not in files:
        return [f"{path} is missing"]
    try:
        manifest = yaml.safe_load(files[path])
    except yaml.YAMLError as exc:
        return [f"{path} is not valid YAML: {exc}"]
    services = manifest.get("services") if isinstance(manifest, dict) else None
    if not isinstance(services, list) or not services:
        return [f"{path} declares no services"]
    problems: list[str] = []
    for number, service in enumerate(services, start=1):
        if not isinstance(service, dict):
            problems.append(f"{path}: service {number} is not a mapping")
            continue
        missing = [k for k in ("type", "name", "runtime") if not service.get(k)]
        if missing:
            problems.append(f"{path}: service {number} has no {', '.join(missing)}")
        if service.get("runtime") == "docker":
            problems += _dockerfile_problems(files, folder,
                                             str(service.get("dockerfilePath", "Dockerfile"))
                                             .removeprefix("./"))
            continue
        start = str(service.get("startCommand", ""))
        refs = _referenced_files(start)
        if not refs:
            problems.append(f"{path}: startCommand {start!r} does not name a Python file")
        build = shlex.split(str(service.get("buildCommand", "")))
        if "-r" in build[:-1]:
            refs.add(build[build.index("-r") + 1])
        for ref in sorted(refs):
            if f"{folder}/{ref}" not in files:
                problems.append(f"{path}: service {service.get('name', number)} needs {ref}, "
                                f"which is not in {folder}/")
    return problems
