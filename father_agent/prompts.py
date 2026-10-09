"""Versioned prompt templates.

Prompts live as Markdown files in ``prompts/`` so they can be read, diffed and
improved without touching code. Each file starts with two HTML comments: the
task marker (``father-agent-task: ...``), which the mock provider uses to know
what it is being asked, and the prompt version, which is logged with every run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

from .errors import ConfigError

logger = logging.getLogger(__name__)

PROMPT_FILES = {"planner": "planner.md", "coder_agent": "coder_agent.md",
                "coder_tests": "coder_tests.md"}
_VERSION_RE = re.compile(r"<!--\s*prompt-version:\s*([^\s]+)\s*-->")
_TASK_RE = re.compile(r"<!--\s*father-agent-task:\s*(.+?)\s*-->")


@dataclass(frozen=True)
class PromptTemplate:
    """One prompt file, parsed."""

    key: str
    version: str
    task: str
    template: Template

    def render(self, **values: str) -> str:
        """Fill ``$placeholders``; missing values raise ConfigError."""
        try:
            return self.template.substitute(values)
        except KeyError as exc:
            raise ConfigError(f"prompt {self.key} needs a value for {exc}") from exc


class PromptLibrary:
    """Loads and renders the prompt templates in a directory."""

    def __init__(self, directory: Path) -> None:
        """Load every prompt in ``directory``.

        Raises:
            ConfigError: If a prompt file is missing or has no version header.
        """
        self.directory = directory
        self._prompts: dict[str, PromptTemplate] = {}
        for key, filename in PROMPT_FILES.items():
            path = directory / filename
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read prompt {path}: {exc}") from exc
            version, task = _VERSION_RE.search(text), _TASK_RE.search(text)
            if not version or not task:
                raise ConfigError(f"prompt {path} is missing its task/version header")
            self._prompts[key] = PromptTemplate(key, version.group(1), task.group(1),
                                                Template(text))
        logger.debug("loaded prompts: %s", self.versions)

    @property
    def versions(self) -> dict[str, str]:
        """``key -> version`` for every loaded prompt."""
        return {k: p.version for k, p in self._prompts.items()}

    def get(self, key: str) -> PromptTemplate:
        """Return one prompt template by key."""
        try:
            return self._prompts[key]
        except KeyError as exc:
            raise ConfigError(f"unknown prompt {key!r}") from exc

    def render(self, key: str, **values: str) -> str:
        """Render prompt ``key`` with ``values``."""
        return self.get(key).render(**values)
