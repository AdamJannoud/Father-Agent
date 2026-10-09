"""The offline, deterministic provider.

It answers the same prompts a real model gets, but from keyword rules and
code templates instead of a network call. That keeps the whole factory
provable with no key and no network: the test suite, ``doctor`` and a fresh
clone all work before you have added a single free key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from ..errors import ProviderError
from ..heuristics import plan_spec
from ..spec import SubAgentSpec
from ..templates import render_agent, render_interface_file, render_tests
from .base import Completion, Message, Provider

logger = logging.getLogger(__name__)

_TASK_RE = re.compile(r"father-agent-task:\s*([a-z]+)(?:\s+file=([\w.]+))?")
_COMMAND_RE = re.compile(r"<command>\s*(.*?)\s*</command>", re.S)
_SPEC_RE = re.compile(r"<spec>\s*(\{.*?\})\s*</spec>", re.S)


class MockProvider(Provider):
    """Deterministic local stand-in for a chat model."""

    name = "mock"
    model = "offline-templates"
    is_mock = True

    def __init__(self, latency: float = 0.0) -> None:
        """Create the mock; ``latency`` simulates a slow call in tests."""
        self.latency = latency
        self.calls: list[str] = []

    async def complete(self, messages: list[Message], *, temperature: float = 0.2,
                       max_tokens: int = 4096) -> Completion:
        """Answer a planner or coder prompt deterministically."""
        if self.latency:
            await asyncio.sleep(self.latency)
        prompt = "\n".join(m.get("content", "") for m in messages)
        match = _TASK_RE.search(prompt)
        if not match:
            raise ProviderError("mock provider got a prompt without a task marker",
                                provider=self.name)
        task, filename = match.group(1), match.group(2) or ""
        self.calls.append(f"{task}:{filename}" if filename else task)
        try:
            text = self._answer(task, filename, prompt)
        except (ValueError, KeyError) as exc:
            raise ProviderError(f"mock provider could not answer: {exc}",
                                provider=self.name) from exc
        logger.debug("mock answered %s %s (%d chars)", task, filename, len(text))
        return Completion(text=text, provider=self.name, model=self.model)

    def _answer(self, task: str, filename: str, prompt: str) -> str:
        """Dispatch on the task marker."""
        if task == "plan":
            command = _COMMAND_RE.search(prompt)
            if not command:
                raise ValueError("planner prompt has no <command> block")
            return "```json\n" + json.dumps(plan_spec(command.group(1)), indent=2) + "\n```"
        if task == "code":
            spec_match = _SPEC_RE.search(prompt)
            if not spec_match:
                raise ValueError("coder prompt has no <spec> block")
            spec = SubAgentSpec.model_validate_json(spec_match.group(1))
            if filename == "agent.py":
                return "```python\n" + render_agent(spec) + "```"
            if filename == "test_agent.py":
                return "```python\n" + render_tests(spec) + "```"
            if filename in ("app.py", "bot.py"):
                return "```python\n" + render_interface_file(spec) + "```"
            raise ValueError(f"unknown file {filename!r}")
        raise ValueError(f"unknown task {task!r}")
