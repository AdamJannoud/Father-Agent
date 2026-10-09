"""The delivery layer: interfaces, the static kit, the three bundle checks, deploy."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from father_agent.cli import main
from father_agent.config import Config
from father_agent.delivery import (
    apply_delivery,
    choose_delivery,
    expected_files,
    infer_delivery,
    render_kit,
)
from father_agent.errors import FatherAgentError, SpecError
from father_agent.factory import BUNDLE_FILES, LEGACY_FILES, Factory
from father_agent.heuristics import plan_spec
from father_agent.providers import MockProvider, ProviderChain
from father_agent.providers.base import Completion
from father_agent.spec import Delivery, SubAgentSpec, parse_spec
from father_agent.templates import render_agent, render_interface_file, render_tests
from father_agent.validator import Validator, gate_summary

from .conftest import SAMPLE_COMMAND

TELEGRAM = "a Telegram bot that watches a Solana wallet and DMs me on changes"
DASHBOARD = "a dashboard that tracks Solana priority fees and plots the last hour"
FASTAPI_WEB = "a FastAPI dashboard of the bitcoin price API"
SERVICE = "a service that exposes the bitcoin price over an HTTP API"


def run(factory: Factory, *args, **kwargs):
    async def go():
        try:
            return await factory.generate(*args, **kwargs)
        finally:
            await factory.aclose()
    return asyncio.run(go())


def mock_bundle(command: str, **delivery: str) -> tuple[SubAgentSpec, dict[str, str]]:
    """Everything the factory would write for ``command``, rendered in memory."""
    spec = SubAgentSpec.model_validate(plan_spec(command))
    if delivery:
        spec.delivery = choose_delivery(spec.delivery, **delivery)
    apply_delivery(spec)
    code = {"agent.py": render_agent(spec), "test_agent.py": render_tests(spec)}
    if spec.delivery.interface != "cli":
        name = "bot.py" if spec.delivery.interface == "telegram" else "app.py"
        code[name] = render_interface_file(spec)
    files = {**code, "README.md": "# readme\n", "spec.json": spec.to_json(),
             **render_kit(spec, code)}
    return spec, files


def gate(spec: SubAgentSpec, files: dict[str, str]) -> dict[str, str]:
    """``check name -> problems text`` for every failed check (empty when all pass)."""
    reports = asyncio.run(Validator().validate_bundle(files, spec))
    return {c.name: c.detail for r in reports for c in r.checks if not c.ok}


# --------------------------------------------------------------------------- #
# Interface: inferred, overridable, backwards compatible
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("command", "interface", "framework"), [
    (TELEGRAM, "telegram", "aiogram"),
    (DASHBOARD, "web", "streamlit"),
    (FASTAPI_WEB, "web", "fastapi"),
    (SERVICE, "api", "fastapi"),
    ("a FastAPI microservice that returns the weather", "api", "fastapi"),
    (SAMPLE_COMMAND, "cli", "argparse"),
    ("track the bitcoin price API every 5 minutes", "cli", "argparse"),
    ("scrape hacker news headlines hourly", "cli", "argparse"),
])
def test_interface_is_read_from_the_command(command: str, interface: str,
                                            framework: str) -> None:
    """The sentence names the interface; consuming an API is not serving one."""
    delivery = infer_delivery(command)
    assert (delivery.interface, delivery.framework) == (interface, framework)
    assert delivery.deploy[0] == "docker"


def test_overrides() -> None:
    """--framework alone picks its interface; --interface resets the framework."""
    cli = Delivery()
    assert choose_delivery(cli, framework="aiogram").interface == "telegram"
    assert choose_delivery(cli, framework="streamlit").label == "web · streamlit"
    web = choose_delivery(cli, interface="web", framework="fastapi")
    assert choose_delivery(web, framework="fastapi").interface == "web"
    assert choose_delivery(web, interface="api").label == "api · fastapi"
    assert choose_delivery(cli) is cli
    with pytest.raises(FatherAgentError, match="does not fit"):
        choose_delivery(cli, interface="telegram", framework="streamlit")


def test_delivery_block_is_validated() -> None:
    """Defaults fill in, aliases normalise, impossible combinations are refused."""
    assert Delivery(interface="web").deploy == ["docker", "hf-spaces", "render"]
    assert Delivery(interface="web", deploy=["huggingface"]).deploy == ["docker", "hf-spaces"]
    assert Delivery(interface="cli", framework="none").framework == "argparse"
    base = {"name": "Demo", "slug": "demo_bot", "summary": "A demo sub-agent for tests.",
            "command": "demo", "domain": "web_api",
            "classes": [{"name": "DemoAgent", "methods": ["run_once"]}]}
    with pytest.raises(SpecError, match="delivery"):
        parse_spec(json.dumps({**base, "delivery": {"interface": "telegram",
                                                    "deploy": ["hf-spaces"]}}), command="d")
    with pytest.raises(SpecError, match="delivery"):
        parse_spec(json.dumps({**base, "delivery": {"interface": "fax"}}), command="d")


def test_spec_without_delivery_block_is_cli() -> None:
    """An old spec.json loads unchanged as a cli sub-agent."""
    data = plan_spec(SAMPLE_COMMAND)
    data.pop("delivery")
    spec = SubAgentSpec.model_validate(data)
    assert spec.delivery.interface == "cli" and not spec.has_delivery_block
    before = spec.model_dump()
    apply_delivery(spec)
    assert spec.model_dump() == before
    assert SubAgentSpec.model_validate(plan_spec(SAMPLE_COMMAND)).has_delivery_block


def test_apply_delivery_is_idempotent() -> None:
    """Framework packages and interface variables are added once."""
    spec = apply_delivery(apply_delivery(SubAgentSpec.model_validate(plan_spec(TELEGRAM))))
    packages = [d.package for d in spec.dependencies]
    assert packages.count("aiogram") == 1 and "aiohttp" in packages
    names = [e.name for e in spec.env_vars]
    assert names.count("BOT_TOKEN") == 1
    assert next(e for e in spec.env_vars if e.name == "BOT_TOKEN").required


# --------------------------------------------------------------------------- #
# The factory writes a complete bundle for every interface, offline
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("command", "entry"), [
    (TELEGRAM, "bot.py"), (DASHBOARD, "app.py"), (FASTAPI_WEB, "app.py"), (SERVICE, "app.py"),
])
def test_every_interface_writes_a_deployable_bundle(command: str, entry: str, config: Config,
                                                    tmp_path: Path) -> None:
    """Mock provider, no key: interface file, kit and deploy folders, all through the gate."""
    provider = MockProvider()
    result = run(Factory(config, ProviderChain([provider])), command, output_dir=tmp_path)
    spec = result.spec
    on_disk = sorted(p.relative_to(result.target_dir).as_posix()
                     for p in result.target_dir.rglob("*") if p.is_file())
    assert on_disk == sorted(expected_files(spec, BUNDLE_FILES))
    assert provider.calls == ["plan", "code:agent.py", f"code:{entry}", "code:test_agent.py"]
    names = {c.name for r in result.reports for c in r.checks}
    assert {"bundle", "deploy", "secrets"} <= names and all(r.ok for r in result.reports)
    saved = json.loads((result.target_dir / "spec.json").read_text())
    assert saved["delivery"]["interface"] == spec.delivery.interface
    readme = (result.target_dir / "README.md").read_text()
    assert "## Deploy" in readme and "docker build" in readme
    assert "BOT_TOKEN=" not in (result.target_dir / "Dockerfile").read_text()


def test_cli_bundle_is_the_common_kit_only(config: Config, tmp_path: Path) -> None:
    """A cli sub-agent gets the shared kit and nothing else; three model calls as before."""
    result = run(Factory(config), SAMPLE_COMMAND, output_dir=tmp_path)
    assert sorted(p.name for p in result.target_dir.iterdir()) == sorted(BUNDLE_FILES)
    requirements = (result.target_dir / "requirements.txt").read_text()
    assert "httpx==" in requirements and "matplotlib==" in requirements
    assert '"python", "agent.py"' in (result.target_dir / "Dockerfile").read_text()
    env = (result.target_dir / ".env.example").read_text()
    assert "\nWALLET_ADDRESS=\n" in env and "\n# WALLET_WATCHER_INTERVAL=\n" in env


def test_interface_override_and_legacy_rebuild(config: Config, tmp_path: Path) -> None:
    """--interface turns the sample into a bot; an old spec rebuilds as cli."""
    bot = run(Factory(config), SAMPLE_COMMAND, output_dir=tmp_path / "a", interface="telegram")
    assert (bot.target_dir / "bot.py").is_file() and bot.spec.delivery.interface == "telegram"
    old = SubAgentSpec.model_validate({k: v for k, v in plan_spec(TELEGRAM).items()
                                       if k != "delivery"})
    rebuilt = run(Factory(config), output_dir=tmp_path / "b", spec=old)
    assert rebuilt.spec.delivery.interface == "cli"
    assert not (rebuilt.target_dir / "bot.py").exists()


class SilentPlanner(MockProvider):
    """A model that plans without a delivery block (an older or terser model)."""

    async def complete(self, messages, **kwargs) -> Completion:
        reply = await super().complete(messages, **kwargs)
        if self.calls[-1] == "plan":
            data = json.loads(reply.text.strip("`").removeprefix("json"))
            data.pop("delivery")
            return Completion(json.dumps(data), self.name, self.model)
        return reply


def test_missing_delivery_from_the_model_is_inferred(config: Config, tmp_path: Path) -> None:
    """When the planner omits delivery, the factory reads it from the sentence."""
    result = run(Factory(config, ProviderChain([SilentPlanner()])), TELEGRAM,
                 output_dir=tmp_path)
    assert result.spec.delivery.label == "telegram · aiogram"


# --------------------------------------------------------------------------- #
# The three new gate checks
# --------------------------------------------------------------------------- #


def test_mock_bundles_pass_every_check() -> None:
    """Every interface's in-memory bundle passes all nine checks."""
    for command in (SAMPLE_COMMAND, TELEGRAM, DASHBOARD, FASTAPI_WEB, SERVICE):
        spec, files = mock_bundle(command)
        reports = asyncio.run(Validator().validate_bundle(files, spec))
        assert all(r.ok for r in reports), gate(spec, files)
        assert gate_summary(reports).endswith("bundle ok · deploy ok · secrets ok")


def test_bundle_check_catches_missing_requirements_and_variables() -> None:
    """An import without a requirement, or a variable missing from .env.example, fails."""
    spec, files = mock_bundle(TELEGRAM)
    files["requirements.txt"] = files["requirements.txt"].replace("aiohttp==", "#aiohttp==")
    files[".env.example"] = files[".env.example"].replace("BOT_TOKEN=", "")
    problems = gate(spec, files)
    assert "bot.py imports 'aiohttp'" in problems["bundle"]
    assert "declared variable BOT_TOKEN" in problems["bundle"]


def test_deploy_check_catches_broken_manifests() -> None:
    """A CMD naming a missing file, bad YAML, or a stale deploy copy fails the gate."""
    spec, files = mock_bundle(DASHBOARD)
    files["Dockerfile"] = files["Dockerfile"].replace("app.py", "web.py")
    files["deploy/render/render.yaml"] = "services: [unclosed\n"
    files["deploy/huggingface/app.py"] += "# edited\n"
    files["deploy/huggingface/README.md"] = files["deploy/huggingface/README.md"].replace(
        "sdk: docker", "sdk: streamlit")
    problems = gate(spec, files)["deploy"]
    assert "CMD runs web.py" in problems
    assert "render.yaml is not valid YAML" in problems
    assert "deploy/huggingface/app.py differs" in problems
    assert "sdk 'streamlit'" in problems


def test_deploy_check_requires_the_interface_file() -> None:
    """A telegram bundle without bot.py or setup_bot.py is not deployable."""
    spec, files = mock_bundle(TELEGRAM)
    del files["scripts/setup_bot.py"]
    files["deploy/render/render.yaml"] = files["deploy/render/render.yaml"].replace(
        "python bot.py", "python worker.py")
    problems = gate(spec, files)["deploy"]
    assert "scripts/setup_bot.py is missing" in problems and "needs worker.py" in problems


@pytest.mark.parametrize("literal", [
    "1234567890:" + "AAH" + "x" * 32,          # Telegram bot token shape
    "gsk_" + "A1b2" * 8,                        # Groq key shape
    "hf_" + "Zq9" * 12,                         # Hugging Face token shape
    "deadbeef" * 5,                             # long hex string
])
def test_secrets_check_refuses_credential_literals(literal: str) -> None:
    """A token pasted into the source, anywhere in the bundle, fails the gate."""
    spec, files = mock_bundle(TELEGRAM)
    files["bot.py"] = files["bot.py"].replace('WEBHOOK_PATH = "/telegram/webhook"',
                                              f'WEBHOOK_PATH = "/telegram/webhook"\n'
                                              f'TOKEN = "{literal}"')
    assert "bot.py" in gate(spec, files)["secrets"]


def test_secrets_check_refuses_filled_env_example() -> None:
    """.env.example must stay empty, commented or not."""
    spec, files = mock_bundle(SAMPLE_COMMAND)
    files[".env.example"] = files[".env.example"].replace("WALLET_ADDRESS=", "WALLET_ADDRESS=x")
    assert "WALLET_ADDRESS has a value" in gate(spec, files)["secrets"]


def test_python_only_bundles_skip_the_delivery_checks() -> None:
    """validate_bundle on code alone (as before the delivery layer) runs checks 1-6 only."""
    spec, files = mock_bundle(SAMPLE_COMMAND)
    code = {n: files[n] for n in ("agent.py", "test_agent.py")}
    reports = asyncio.run(Validator().validate_bundle(code, spec))
    assert [r.filename for r in reports] == ["agent.py", "test_agent.py"]


# --------------------------------------------------------------------------- #
# bootstrap.py, the CLI and main.py deploy
# --------------------------------------------------------------------------- #


def test_bootstrap_installs_nothing_unless_asked(tmp_path: Path) -> None:
    """A missing declared package is reported; pip is not run without the opt-in."""
    _, files = mock_bundle(SAMPLE_COMMAND)
    (tmp_path / "bootstrap.py").write_text(files["bootstrap.py"])
    (tmp_path / "requirements.txt").write_text("father-agent-no-such-package==1.0  # test\n")
    env = {"PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, "bootstrap.py"], cwd=tmp_path, capture_output=True,
                          text=True, timeout=60, env=env)
    assert proc.returncode == 1
    assert "father-agent-no-such-package==1.0" in proc.stderr
    assert "nothing installed" in proc.stderr and "running:" not in proc.stderr


def test_cli_new_show_and_deploy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """new --interface/--framework, show re-checks nine checks, deploy prepares and prints."""
    out = tmp_path / "agents"
    assert main(["new", "-o", str(out), "--interface", "web", "--framework", "fastapi",
                 SAMPLE_COMMAND]) == 0
    text = capsys.readouterr().out
    assert "interface web · fastapi" in text and "father delivery 5/6" in text
    assert "father write    6/6" in text

    assert main(["show", "wallet_watcher", "-o", str(out)]) == 0
    shown = capsys.readouterr().out
    assert "web · fastapi" in shown and "secrets ok" in shown and "MISSING" not in shown

    folder = out / "wallet_watcher"
    (folder / "app.py").write_text((folder / "app.py").read_text() + "# tweaked\n")
    assert main(["deploy", "wallet_watcher", "-o", str(out), "--target", "render"]) == 0
    deployed = capsys.readouterr().out
    assert "dashboard.render.com" in deployed and "deploy ok" in deployed
    assert (folder / "deploy/render/app.py").read_text().endswith("# tweaked\n")

    assert main(["deploy", "wallet_watcher", "-o", str(out), "--target", "docker",
                 "--no-build"]) == 0
    assert "docker build -t wallet-watcher ." in capsys.readouterr().out

    (folder / "bot.py").write_text('"""x"""\nTOKEN = "' + "ab12" * 10 + '"\n')
    assert main(["deploy", "wallet_watcher", "-o", str(out), "--target", "hf-spaces"]) == 1
    assert "long hex string" in capsys.readouterr().err


def test_cli_rejects_targets_and_legacy_bundles(tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    """deploy refuses a target the interface lacks; show still accepts an old bundle."""
    out = tmp_path / "agents"
    assert main(["new", "-o", str(out), SAMPLE_COMMAND]) == 0
    capsys.readouterr()
    assert main(["deploy", "wallet_watcher", "-o", str(out), "--target", "render"]) == 2
    assert "prepared for docker" in capsys.readouterr().err

    folder = out / "wallet_watcher"
    for path in folder.iterdir():
        if path.name not in LEGACY_FILES:
            path.unlink()
    spec = json.loads((folder / "spec.json").read_text())
    spec.pop("delivery")
    (folder / "spec.json").write_text(json.dumps(spec))
    assert main(["show", "wallet_watcher", "-o", str(out)]) == 0
    assert "predates the delivery layer" in capsys.readouterr().out
    assert main(["deploy", "wallet_watcher", "-o", str(out), "--target", "docker"]) == 2
    assert "--from-spec" in capsys.readouterr().err
