"""The validator gate: every check, pass and fail."""

from __future__ import annotations

import asyncio

import pytest

from father_agent.heuristics import plan_spec
from father_agent.spec import SubAgentSpec
from father_agent.templates import render_agent, render_tests
from father_agent.validator import Validator, find_ruff

from .conftest import SAMPLE_COMMAND

GOOD = '''"""A tiny agent."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class DemoAgent:
    """Does one thing."""

    async def run_once(self) -> int:
        """Fetch once."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://example.org")
        except httpx.HTTPError as exc:
            logger.error("failed: %s", exc)
            return 0
        return response.status_code


def main() -> int:
    """Entry point."""
    return asyncio.run(DemoAgent().run_once())
'''


@pytest.fixture
def spec() -> SubAgentSpec:
    return SubAgentSpec.model_validate({
        "name": "Demo", "slug": "demo_agent", "summary": "A demo sub-agent for tests.",
        "command": "demo", "domain": "web_api",
        "dependencies": [{"package": "httpx"}],
        "classes": [{"name": "DemoAgent", "methods": ["run_once"]}]})


def check(source: str, spec: SubAgentSpec, filename: str = "agent.py",
          bundle: dict | None = None, **kwargs: object):
    return asyncio.run(Validator(**kwargs).validate_file(filename, source, spec=spec,
                                                         bundle=bundle))


def test_good_file_passes_every_check(spec: SubAgentSpec) -> None:
    report = check(GOOD, spec)
    assert report.ok, report.problems
    names = [c.name for c in report.checks]
    assert names == ["ast", "compile", "ruff", "imports", "consistency", "policy"]


def test_syntax_error_stops_at_ast(spec: SubAgentSpec) -> None:
    report = check("def broken(:\n    pass\n", spec)
    assert not report.ok
    assert [c.name for c in report.checks] == ["ast"]
    assert "line 1" in report.problems[0]


def test_undeclared_import_fails(spec: SubAgentSpec) -> None:
    report = check(GOOD.replace("import httpx\n", "import httpx\nimport requests\n"), spec)
    assert any("requests" in p and p.startswith("imports") for p in report.problems)


def test_paid_sdk_import_fails(spec: SubAgentSpec) -> None:
    report = check(GOOD.replace("import httpx\n", "import httpx\nimport openai\n"), spec)
    assert any("paid-API SDK" in p for p in report.problems)


def test_missing_spec_class_and_method_fail(spec: SubAgentSpec) -> None:
    report = check(GOOD.replace("async def run_once", "async def go"), spec)
    assert any("missing method(s): run_once" in p for p in report.problems)
    report = check(GOOD.replace("def main()", "def start()"), spec)
    assert any("main()" in p for p in report.problems)


def test_policy_rejects_print_eval_and_missing_docstrings(spec: SubAgentSpec) -> None:
    src = GOOD.replace('        """Fetch once."""\n', '        print("hi")\n        eval("1")\n')
    problems = check(src, spec, use_ruff=False).problems
    assert any("print()" in p for p in problems)
    assert any("eval()" in p for p in problems)
    assert any("run_once has no docstring" in p for p in problems)


def test_policy_requires_async_and_try(spec: SubAgentSpec) -> None:
    src = '"""Sync."""\nimport logging\n\n\nclass DemoAgent:\n    """D."""\n\n' \
          '    def run_once(self):\n        """R."""\n        return 1\n\n\n' \
          'def main():\n    """M."""\n    return 0\n'
    problems = check(src, spec).problems
    assert any("async" in p for p in problems)
    assert any("try/except" in p for p in problems)


def test_shell_true_is_refused(spec: SubAgentSpec) -> None:
    src = GOOD.replace("import httpx\n", "import httpx\nimport subprocess\n").replace(
        "        return response.status_code",
        "        subprocess.run('ls', shell=True)\n        return response.status_code")
    assert any("shell=True" in p for p in check(src, spec).problems)


@pytest.mark.skipif(find_ruff() is None, reason="ruff not installed")
def test_ruff_catches_undefined_names(spec: SubAgentSpec) -> None:
    src = GOOD.replace("return response.status_code", "return respnse.status_code")
    report = check(src, spec)
    ruff = next(c for c in report.checks if c.name == "ruff")
    assert not ruff.ok and "F821" in ruff.detail


def test_missing_ruff_is_skipped_or_required(spec: SubAgentSpec) -> None:
    skipped = check(GOOD, spec, use_ruff=False)
    assert skipped.ok and "ruff skipped" in skipped.summary
    required = check(GOOD, spec, use_ruff=False, require_ruff=True)
    assert not required.ok


def test_test_file_may_only_import_defined_names(spec: SubAgentSpec) -> None:
    tests = ('"""T."""\nfrom agent import DemoAgent, Ghost\n\n\n'
             'def test_x():\n    assert DemoAgent and Ghost\n')
    report = check(tests, spec, "test_agent.py", bundle={"agent.py": GOOD})
    assert any("Ghost" in p for p in report.problems)
    assert not any("DemoAgent" in p for p in report.problems)


@pytest.mark.parametrize("command", [
    SAMPLE_COMMAND, "scrape hacker news headlines hourly",
    "train a classifier on a churn csv dataset and chart the score",
    "organize my downloads folder", "track the bitcoin price API every 5 minutes",
    "an ethereum wallet monitor",
])
def test_every_template_passes_the_gate(command: str) -> None:
    """The mock coder's output for every specialisation is gate-clean."""
    spec = SubAgentSpec.model_validate(plan_spec(command))
    bundle = {"agent.py": render_agent(spec), "test_agent.py": render_tests(spec)}
    reports = asyncio.run(Validator().validate_bundle(bundle, spec))
    for report in reports:
        assert report.ok, (report.filename, report.problems)
