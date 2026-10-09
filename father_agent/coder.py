"""Stage 2: write each sub-agent file, one at a time, through the validator.

The coder never trusts a reply: every file is checked by the validator gate
while it is still a string. A rejected file goes back to the model with the
exact problems listed, up to ``max_repairs`` times, and only then fails.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .errors import ValidationFailedError
from .prompts import PromptLibrary
from .providers.chain import ProviderChain
from .spec import SubAgentSpec
from .validator import FileReport, Validator

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    """Return the Python source from a model reply.

    Takes the longest fenced block when there is one, otherwise the whole
    reply. The result always ends with exactly one newline.
    """
    blocks = _FENCE_RE.findall(text)
    code = max(blocks, key=len) if blocks else text
    return code.strip("\n") + "\n"


@dataclass
class GeneratedFile:
    """A validated file, still in memory."""

    filename: str
    source: str
    report: FileReport
    provider: str
    attempts: int

    @property
    def lines(self) -> int:
        """Number of source lines."""
        return self.source.count("\n")


class Coder:
    """Writes agent.py and test_agent.py for a spec."""

    def __init__(self, chain: ProviderChain, prompts: PromptLibrary, validator: Validator, *,
                 max_repairs: int = 2, temperature: float = 0.2) -> None:
        """Create the coder.

        Args:
            chain: Providers to ask.
            prompts: Prompt library holding the coder templates.
            validator: The gate every reply must pass.
            max_repairs: Extra attempts after a rejected file.
            temperature: Sampling temperature for code generation.
        """
        self.chain = chain
        self.prompts = prompts
        self.validator = validator
        self.max_repairs = max(0, max_repairs)
        self.temperature = temperature

    async def write_agent(self, spec: SubAgentSpec) -> GeneratedFile:
        """Generate and validate ``agent.py``."""
        return await self._generate("coder_agent", "agent.py", spec, {})

    async def write_tests(self, spec: SubAgentSpec, agent: GeneratedFile) -> GeneratedFile:
        """Generate and validate ``test_agent.py`` against the finished agent."""
        return await self._generate("coder_tests", "test_agent.py", spec,
                                    {"agent.py": agent.source}, agent_code=agent.source)

    async def _generate(self, prompt_key: str, filename: str, spec: SubAgentSpec,
                        siblings: dict[str, str], **values: str) -> GeneratedFile:
        """Ask, validate, and repair until the file passes or attempts run out."""
        feedback = ""
        problems: list[str] = []
        attempts = self.max_repairs + 1
        for attempt in range(1, attempts + 1):
            prompt = self.prompts.render(prompt_key, spec=spec.model_dump_json(indent=1),
                                         feedback=feedback, **values)
            completion = await self.chain.complete(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": f"Write {filename} now."}],
                temperature=self.temperature, max_tokens=8192)
            source = extract_code(completion.text)
            report = await self.validator.validate_file(
                filename, source, spec=spec, bundle={**siblings, filename: source})
            if report.ok:
                logger.info("%s accepted on attempt %d (%s)", filename, attempt,
                            completion.label)
                return GeneratedFile(filename, source, report, completion.label, attempt)
            problems = report.problems
            logger.warning("%s rejected on attempt %d/%d: %s", filename, attempt, attempts,
                           "; ".join(problems))
            feedback = ("\nYour previous attempt was REJECTED by the validator:\n- "
                        + "\n- ".join(problems[:25])
                        + "\nFix every problem and reply with the whole corrected file.\n")
        raise ValidationFailedError(
            f"{filename} failed the validator gate after {attempts} attempt(s); nothing was "
            f"written. Problems:\n  - " + "\n  - ".join(problems),
            filename=filename, problems=problems)
