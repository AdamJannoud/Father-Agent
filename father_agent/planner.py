"""Stage 1: turn a one-line command into a validated spec."""

from __future__ import annotations

import logging

from .errors import FatherAgentError, SpecError
from .prompts import PromptLibrary
from .providers.base import Completion, Message
from .providers.chain import ProviderChain
from .spec import SubAgentSpec, parse_spec, spec_schema_json

logger = logging.getLogger(__name__)

MAX_COMMAND_CHARS = 2000


class Planner:
    """Asks the model for a JSON spec and checks it against the contract."""

    def __init__(self, chain: ProviderChain, prompts: PromptLibrary, *,
                 max_attempts: int = 2, temperature: float = 0.2) -> None:
        """Create the planner.

        Args:
            chain: Providers to ask.
            prompts: Prompt library holding the ``planner`` template.
            max_attempts: Tries before giving up on a malformed spec.
            temperature: Sampling temperature for the planner call.
        """
        self.chain = chain
        self.prompts = prompts
        self.max_attempts = max(1, max_attempts)
        self.temperature = temperature

    @staticmethod
    def clean_command(command: str) -> str:
        """Normalise whitespace and reject empty or oversized commands."""
        text = " ".join((command or "").split())
        if len(text) < 3:
            raise FatherAgentError("the command is empty; describe the sub-agent you want")
        if len(text) > MAX_COMMAND_CHARS:
            raise FatherAgentError(f"the command is longer than {MAX_COMMAND_CHARS} characters")
        return text

    async def plan(self, command: str) -> tuple[SubAgentSpec, Completion]:
        """Return the spec for ``command`` and the completion that produced it.

        Raises:
            SpecError: When no attempt produced a spec that matches the contract.
        """
        command = self.clean_command(command)
        messages: list[Message] = [
            {"role": "system", "content": self.prompts.render("planner",
                                                              schema=spec_schema_json())},
            {"role": "user", "content": f"<command>\n{command}\n</command>"},
        ]
        last_error: SpecError | None = None
        for attempt in range(1, self.max_attempts + 1):
            completion = await self.chain.complete(messages, temperature=self.temperature,
                                                   max_tokens=2048)
            try:
                spec = parse_spec(completion.text, command=command, planned_by=completion.label)
            except SpecError as exc:
                last_error = exc
                logger.warning("planner attempt %d rejected: %s", attempt, exc)
                messages += [
                    {"role": "assistant", "content": completion.text},
                    {"role": "user", "content": f"That spec was rejected: {exc}. Reply with the "
                                                f"corrected JSON object only."},
                ]
                continue
            logger.info("planned %s (%s) with %s", spec.slug, spec.domain, completion.label)
            return spec, completion
        raise SpecError(f"the planner could not produce a valid spec after "
                        f"{self.max_attempts} attempt(s): {last_error}")
