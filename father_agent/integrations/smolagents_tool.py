"""Expose the Father Agent as a smolagents ``Tool``.

smolagents (by Hugging Face) is the framework the Father Agent is designed to
plug into: a smolagents agent can call this tool to have a new, validated
sub-agent written for it. smolagents is optional; it is imported only when
:func:`make_factory_tool` is called.

Example::

    from smolagents import CodeAgent, InferenceClientModel
    from father_agent.integrations.smolagents_tool import make_factory_tool

    agent = CodeAgent(tools=[make_factory_tool()], model=InferenceClientModel())
    agent.run("Build me a sub-agent that scrapes Hacker News headlines hourly.")
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import FatherAgentError
from ..factory import Factory

logger = logging.getLogger(__name__)


def generate_sync(command: str, config: Config | None = None, *,
                  output_dir: Path | None = None, force: bool = False) -> str:
    """Run the factory synchronously and return a one-line summary for an agent."""
    config = config or Config.load()

    async def _run() -> str:
        factory = Factory(config)
        try:
            result = await factory.generate(command, output_dir=output_dir, force=force)
        finally:
            await factory.aclose()
        return (f"Wrote sub-agent '{result.spec.slug}' to {result.target_dir} "
                f"({', '.join(result.files)}); validated, not executed. "
                f"Run it with: {result.spec.run_example}")

    try:
        return asyncio.run(_run())
    except FatherAgentError as exc:
        logger.error("factory tool failed: %s", exc)
        return f"Father Agent failed: {exc}"


def make_factory_tool(config: Config | None = None, **kwargs: Any) -> Any:
    """Return a smolagents Tool that builds sub-agents from a text command.

    Raises:
        ImportError: If smolagents is not installed (``pip install smolagents``).
    """
    try:
        from smolagents import Tool
    except ImportError as exc:
        raise ImportError("smolagents is not installed: pip install smolagents") from exc

    class FatherAgentTool(Tool):
        """smolagents wrapper around :func:`generate_sync`."""

        name = "father_agent"
        description = ("Plans, writes and validates a professional async Python sub-agent "
                       "from a one-line English description, and saves it to disk. "
                       "Returns where it was written and how to run it.")
        inputs = {"command": {"type": "string",
                              "description": "What the sub-agent should do, in plain English."}}
        output_type = "string"

        def forward(self, command: str) -> str:
            """Build the sub-agent described by ``command``."""
            return generate_sync(command, config, **kwargs)

    return FatherAgentTool()
