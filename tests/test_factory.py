"""End-to-end factory runs through the mock provider (no key, no network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from father_agent.config import Config
from father_agent.errors import OutputExistsError, ValidationFailedError
from father_agent.factory import BUNDLE_FILES, Factory, load_spec
from father_agent.providers import MockProvider, ProviderChain
from father_agent.providers.base import Completion

from .conftest import SAMPLE_COMMAND


def run(factory: Factory, *args, **kwargs):
    async def go():
        try:
            return await factory.generate(*args, **kwargs)
        finally:
            await factory.aclose()
    return asyncio.run(go())


class BrokenCoder(MockProvider):
    """Plans correctly, then writes agent.py with a syntax error `broken_first` times."""

    def __init__(self, broken_first: int) -> None:
        super().__init__()
        self.broken_left = broken_first
        self.seen_feedback: list[str] = []

    async def complete(self, messages, **kwargs) -> Completion:
        prompt = messages[0]["content"]
        if "file=agent.py" in prompt:
            self.seen_feedback.append(prompt)
            if self.broken_left > 0:
                self.broken_left -= 1
                return Completion("```python\ndef oops(:\n    pass\n```", self.name, self.model)
        return await super().complete(messages, **kwargs)


def test_sample_command_writes_validated_bundle(config: Config, tmp_path: Path) -> None:
    result = run(Factory(config), SAMPLE_COMMAND, output_dir=tmp_path / "out")
    target = tmp_path / "out" / "wallet_watcher"
    assert result.target_dir == target
    assert sorted(p.name for p in target.iterdir()) == sorted(BUNDLE_FILES)
    assert all(r.ok for r in result.reports)
    agent = (target / "agent.py").read_text()
    for name in ("class WalletWatcher", "class BalanceStore", "class BalancePlotter",
                 "async def run_once", "import httpx", "logging.getLogger"):
        assert name in agent
    spec = json.loads((target / "spec.json").read_text())
    assert spec["planned_by"] == "mock/offline-templates"
    readme = (target / "README.md").read_text()
    assert "WALLET_ADDRESS" in readme and "never" in readme
    # no staging/backup folders left behind
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["wallet_watcher"]


def test_dry_run_writes_nothing(config: Config, tmp_path: Path) -> None:
    result = run(Factory(config), SAMPLE_COMMAND, output_dir=tmp_path / "out", dry_run=True)
    assert result.dry_run and not result.written
    assert not (tmp_path / "out").exists()


def test_existing_folder_needs_force(config: Config, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run(Factory(config), SAMPLE_COMMAND, output_dir=out)
    (out / "wallet_watcher" / "agent.py").write_text("# edited by hand\n")
    with pytest.raises(OutputExistsError, match="--force"):
        run(Factory(config), SAMPLE_COMMAND, output_dir=out)
    assert (out / "wallet_watcher" / "agent.py").read_text() == "# edited by hand\n"
    run(Factory(config), SAMPLE_COMMAND, output_dir=out, force=True)
    assert "class WalletWatcher" in (out / "wallet_watcher" / "agent.py").read_text()


def test_invalid_code_is_never_written(config: Config, tmp_path: Path) -> None:
    provider = BrokenCoder(broken_first=99)
    factory = Factory(config, ProviderChain([provider]))
    with pytest.raises(ValidationFailedError) as info:
        run(factory, SAMPLE_COMMAND, output_dir=tmp_path / "out")
    assert info.value.filename == "agent.py"
    assert "nothing was written" in str(info.value)
    assert "ast: line 1" in str(info.value)
    assert not (tmp_path / "out").exists()


def test_rejected_file_is_repaired_with_feedback(config: Config, tmp_path: Path) -> None:
    provider = BrokenCoder(broken_first=1)
    result = run(Factory(config, ProviderChain([provider])), SAMPLE_COMMAND,
                 output_dir=tmp_path / "out")
    assert result.written
    assert "REJECTED by the validator" in provider.seen_feedback[1]
    assert "REJECTED" not in provider.seen_feedback[0]


def test_rebuild_from_spec_skips_planning(config: Config, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run(Factory(config), SAMPLE_COMMAND, output_dir=out)
    spec = load_spec(out / "wallet_watcher")
    provider = MockProvider()
    run(Factory(config, ProviderChain([provider])), output_dir=tmp_path / "again", spec=spec)
    assert provider.calls == ["code:agent.py", "code:test_agent.py"]
    assert (tmp_path / "again" / "wallet_watcher" / "agent.py").is_file()


def test_empty_command_is_rejected(config: Config, tmp_path: Path) -> None:
    from father_agent.errors import FatherAgentError
    with pytest.raises(FatherAgentError, match="empty"):
        run(Factory(config), "  ", output_dir=tmp_path / "out")


def test_remote_http_path_end_to_end(config: Config, tmp_path: Path) -> None:
    """The real OpenAI-compatible HTTP path, answered in-process (no network)."""
    import httpx

    from father_agent.providers import OpenAICompatProvider

    brain = MockProvider()
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        reply = await brain.complete(body["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"content": reply.text}}]})

    groq = OpenAICompatProvider("groq", config.groq_base_url, "qwen/qwen3.8-27b", "gsk_test",
                                transport=httpx.MockTransport(handler))
    result = run(Factory(config, ProviderChain([groq])), SAMPLE_COMMAND,
                 output_dir=tmp_path / "out")
    assert result.spec.planned_by == "groq/qwen/qwen3.8-27b"
    assert len(requests) == 3 and all(r["model"] == "qwen/qwen3.8-27b" for r in requests)
    assert "using `groq/qwen/qwen3.8-27b`" in (result.target_dir / "README.md").read_text()
