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
"""

from __future__ import annotations

import ast
import asyncio
import logging
import py_compile
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .spec import PAID_SDKS, SubAgentSpec

logger = logging.getLogger(__name__)

RUFF_RULES = "E9,F,B"
MAX_SOURCE_BYTES = 200_000
#: Modules every generated test file may import besides the spec's dependencies.
TEST_ALLOWED = frozenset({"pytest", "_pytest"})
_BANNED_CALLS = {"eval", "exec", "__import__", "compile"}
_BANNED_ATTR_CALLS = {("os", "system"), ("os", "popen"), ("pickle", "loads")}


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
        """Validate every file of a sub-agent concurrently."""
        return list(await asyncio.gather(
            *(self.validate_file(name, src, spec=spec, bundle=files)
              for name, src in files.items())
        ))

    async def validate_file(self, filename: str, source: str, *, spec: SubAgentSpec,
                            bundle: dict[str, str] | None = None) -> FileReport:
        """Run every check on one file and return the report."""
        role = "test" if Path(filename).name.startswith("test_") else "agent"
        report = FileReport(filename, role)
        bundle = bundle or {filename: source}

        tree = self._check_ast(filename, source, report)
        if tree is None:
            return report
        report.checks.append(await asyncio.to_thread(self._check_compile, filename, source))
        report.checks.append(await self._check_ruff(filename, source))
        report.checks.append(self._check_imports(tree, role, spec, bundle))
        report.checks.append(self._check_consistency(tree, role, spec, bundle))
        report.checks.append(self._check_policy(tree, role))
        logger.info("validated %s: %s", filename, report.summary)
        return report

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
                           bundle: dict[str, str]) -> CheckResult:
        """Check promised classes/methods/entry point, and test-to-agent imports."""
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
        else:
            agent_src = bundle.get("agent.py")
            if agent_src is not None:
                try:
                    agent_names = _module_level_names(ast.parse(agent_src))
                except (SyntaxError, ValueError):
                    agent_names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module == "agent":
                        for alias in node.names:
                            if alias.name != "*" and alias.name not in agent_names:
                                problems.append(f"line {node.lineno}: imports {alias.name!r} "
                                                f"from agent, which agent.py does not define")
            if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name.startswith("test") for n in ast.walk(tree)):
                problems.append("no test functions found")
        return CheckResult("consistency", not problems, "\n".join(problems))

    def _check_policy(self, tree: ast.Module, role: str) -> CheckResult:
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
            if role == "agent" and isinstance(func, ast.Name) and func.id == "print":
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
                if role == "agent" and public and not ast.get_docstring(item):
                    problems.append(f"line {item.lineno}: {item.name} has no docstring")

        if role == "agent":
            nodes = list(ast.walk(tree))
            if not any(isinstance(n, ast.AsyncFunctionDef) for n in nodes):
                problems.append("no async def: I/O must use asyncio")
            if not any(isinstance(n, ast.Try) for n in nodes):
                problems.append("no try/except: external calls must handle errors")
            if not any(isinstance(n, ast.Import) and any(a.name == "logging" for a in n.names)
                       for n in nodes):
                problems.append("logging is not imported")
        return CheckResult("policy", not problems, "\n".join(problems))
