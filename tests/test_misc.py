"""Logging redaction, prompt library, code extraction, smolagents tool."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from father_agent.coder import extract_code
from father_agent.config import Config
from father_agent.logging_setup import LOG_FILE_NAME, setup_logging
from father_agent.prompts import PromptLibrary

from .conftest import ROOT


def test_keys_are_redacted_from_the_log_file(config: Config) -> None:
    secret = "gsk_supersecretvalue_9876"
    keyed = Config(**{**config.__dict__, "groq_api_key": secret})
    path = setup_logging(keyed, quiet=True)
    logging.getLogger("father_agent.test").warning("calling with key %s", secret)
    for handler in logging.getLogger("father_agent").handlers:
        handler.flush()
    text = Path(path).read_text()
    assert "[REDACTED]" in text and secret not in text
    assert path.name == LOG_FILE_NAME


def test_prompt_library_versions_and_render() -> None:
    lib = PromptLibrary(ROOT / "prompts")
    assert set(lib.versions) == {"planner", "coder_agent", "coder_tests", "coder_streamlit",
                                 "coder_fastapi", "coder_telegram"}
    rendered = lib.render("coder_agent", spec="{}", feedback="")
    assert "father-agent-task: code file=agent.py" in rendered
    for rule in ("async def", "logging", "try/except", "docstring"):
        assert rule in rendered


def test_extract_code_prefers_longest_fence() -> None:
    reply = "Intro\n```python\nx = 1\n```\nand\n```python\nimport os\n\nprint(os)\n```\n"
    assert extract_code(reply) == "import os\n\nprint(os)\n"
    assert extract_code("y = 2") == "y = 2\n"


def test_smolagents_tool_builds_a_subagent(tmp_path: Path, config: Config) -> None:
    pytest.importorskip("smolagents")
    from father_agent.integrations.smolagents_tool import make_factory_tool

    tool = make_factory_tool(config, output_dir=tmp_path)
    assert tool.name == "father_agent" and "command" in tool.inputs
    message = tool.forward("scrape hacker news headlines hourly")
    assert "hacker_news_scraper" in message and "not executed" in message
    assert (tmp_path / "hacker_news_scraper" / "agent.py").is_file()
