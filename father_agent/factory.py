"""The Factory: command in, validated sub-agent folder out.

Five stages, in order, because each needs the one before it::

    1-2  Planner    command  -> spec (checked against the JSON contract)
    3    Coder      spec     -> agent.py, test_agent.py (each through the gate)
    4    Validator  the whole bundle, once more, together
    5    Writer     subagents/<slug>/ — the only stage that touches the disk

Generated code is never imported or executed by the factory.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .coder import Coder
from .config import PROJECT_ROOT, Config
from .errors import FatherAgentError, OutputExistsError, SpecError, ValidationFailedError
from .logging_setup import ProgressReporter
from .planner import Planner
from .prompts import PromptLibrary
from .providers.chain import ProviderChain, build_chain
from .spec import SubAgentSpec
from .templates import render_readme
from .validator import FileReport, Validator

logger = logging.getLogger(__name__)

BUNDLE_FILES = ("agent.py", "test_agent.py", "README.md", "spec.json")


@dataclass
class GenerationResult:
    """What one ``generate`` call produced."""

    spec: SubAgentSpec
    target_dir: Path
    files: dict[str, Path] = field(default_factory=dict)
    reports: list[FileReport] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    dry_run: bool = False
    elapsed: float = 0.0

    @property
    def written(self) -> bool:
        """True when files were written to disk."""
        return bool(self.files)


class Factory:
    """Orchestrates Planner → Coder → Validator → Writer."""

    def __init__(self, config: Config, chain: ProviderChain | None = None, *,
                 validator: Validator | None = None, prompts: PromptLibrary | None = None,
                 reporter: ProgressReporter | None = None) -> None:
        """Wire the stages together.

        Args:
            config: Runtime settings.
            chain: Provider chain; built from ``config`` when None.
            validator: The gate; a default Validator when None.
            prompts: Prompt library; loaded from ``config.prompts_dir`` when None.
            reporter: Progress output; a default ProgressReporter when None.
        """
        self.config = config
        self.reporter = reporter or ProgressReporter()
        self.chain = chain or build_chain(config, on_event=self._provider_event)
        if self.chain.on_event is None:
            self.chain.on_event = self._provider_event
        self.prompts = prompts or PromptLibrary(config.prompts_dir)
        self.validator = validator or Validator()
        self.planner = Planner(self.chain, self.prompts, temperature=config.temperature)
        self.coder = Coder(self.chain, self.prompts, self.validator,
                           max_repairs=config.max_repair_attempts,
                           temperature=config.temperature)
        self._step = (0, "")

    def _provider_event(self, message: str) -> None:
        """Show provider retries and switches against the current stage."""
        number, stage = self._step
        self.reporter.step(stage or "provider", number or None, message)

    def _progress(self, stage: str, number: int, message: str) -> None:
        """Record the current stage and print its progress line."""
        self._step = (number, stage)
        self.reporter.step(stage, number, message)

    @staticmethod
    def run_example(spec: SubAgentSpec, out_root: Path) -> str:
        """How to run the sub-agent, given where it will be written."""
        try:
            rel = out_root.resolve().relative_to(PROJECT_ROOT)
        except ValueError:
            return f"python {out_root / spec.slug / 'agent.py'} --once"
        module = ".".join([*rel.parts, spec.slug, "agent"])
        if all(part.isidentifier() for part in [*rel.parts, spec.slug]):
            return f"python -m {module} --once"
        return f"python {rel / spec.slug / 'agent.py'} --once"

    async def generate(self, command: str = "", *, output_dir: Path | None = None,
                       force: bool = False, dry_run: bool = False,
                       spec: SubAgentSpec | None = None) -> GenerationResult:
        """Plan, write, validate and save one sub-agent.

        Args:
            command: The plain-English command (ignored when ``spec`` is given).
            output_dir: Parent folder for the sub-agent; ``config.output_dir`` by default.
            force: Replace an existing sub-agent folder with the same slug.
            dry_run: Stop after planning; nothing is written.
            spec: Re-use an existing spec (from spec.json) instead of planning.

        Raises:
            FatherAgentError: Any failure, with a message fit for the terminal.
        """
        started = time.monotonic()
        out_root = (output_dir or self.config.output_dir).expanduser()
        if not out_root.is_absolute():
            out_root = Path.cwd() / out_root
        providers: list[str] = []
        try:
            if spec is None:
                self._progress("spec", 1, f"planning · provider {self.chain.primary.label}")
                spec, completion = await self.planner.plan(command)
                providers.append(completion.label)
            else:
                self._progress("spec", 1, f"re-using spec {spec.slug} (planning skipped)")
            spec.run_example = self.run_example(spec, out_root)
            target = out_root / spec.slug
            if target.exists() and not force and not dry_run:
                raise OutputExistsError(f"{target} already exists; pass --force to replace it "
                                        f"or change the command")
            libs = " + ".join(d.package for d in spec.dependencies) or "standard library only"
            self._progress("spec", 2, f"{spec.slug} · {spec.domain} · {libs}")
            if dry_run:
                return GenerationResult(spec, target, providers=providers, dry_run=True,
                                        elapsed=time.monotonic() - started)

            agent = await self.coder.write_agent(spec)
            methods = sum(len(c.methods) for c in spec.classes)
            self._progress("code", 3, f"wrote agent.py: {len(spec.classes)} classes, "
                                      f"{methods} methods, docstrings · {agent.lines} lines")
            tests = await self.coder.write_tests(spec, agent)
            providers += [agent.provider, tests.provider]
            self._progress("code", 3, f"wrote test_agent.py · {tests.lines} lines")

            bundle = {"agent.py": agent.source, "test_agent.py": tests.source}
            reports = await self.validator.validate_bundle(bundle, spec)
            failed = [r for r in reports if not r.ok]
            if failed:
                problems = [f"{r.filename}: {p}" for r in failed for p in r.problems]
                raise ValidationFailedError(
                    "the bundle failed the validator gate; nothing was written:\n  - "
                    + "\n  - ".join(problems), filename=failed[0].filename, problems=problems)
            self._progress("validate", 4, f"{reports[0].summary} · nothing was executed")

            label = agent.provider
            texts = {
                "agent.py": agent.source,
                "test_agent.py": tests.source,
                "README.md": render_readme(spec, provider_label=label),
                "spec.json": self._spec_json(spec),
            }
            files = self._write(target, texts, force=force)
            self._progress("write", 5, f"{self._display(target)}/ · "
                                       f"{' · '.join(BUNDLE_FILES)}")
            self.reporter.note(f"next: {spec.run_example}")
            elapsed = time.monotonic() - started
            logger.info("generated %s in %.1fs via %s", spec.slug, elapsed, ", ".join(providers))
            return GenerationResult(spec, target, files, reports, providers, False, elapsed)
        except FatherAgentError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure leaves as a FatherAgentError
            logger.exception("generation crashed")
            raise FatherAgentError(f"generation failed unexpectedly: {exc}") from exc

    @staticmethod
    def _display(path: Path) -> str:
        """Path relative to the cwd when possible, for short progress lines."""
        try:
            return str(path.resolve().relative_to(Path.cwd().resolve()))
        except ValueError:
            return str(path)

    @staticmethod
    def _spec_json(spec: SubAgentSpec) -> str:
        """spec.json content, checked to round-trip through the contract."""
        text = spec.to_json()
        try:
            SubAgentSpec.model_validate(json.loads(text))
        except (ValueError, TypeError) as exc:
            raise SpecError(f"spec.json would not round-trip: {exc}") from exc
        return text

    @staticmethod
    def _write(target: Path, texts: dict[str, str], *, force: bool) -> dict[str, Path]:
        """Write the bundle atomically: stage in a sibling folder, then rename.

        A crash mid-write leaves either the old folder or the new one, never a
        half-written mix.
        """
        parent = target.parent
        staging = parent / f".{target.name}.staging-{uuid.uuid4().hex[:8]}"
        backup = parent / f".{target.name}.old-{uuid.uuid4().hex[:8]}"
        try:
            parent.mkdir(parents=True, exist_ok=True)
            staging.mkdir()
            for name, text in texts.items():
                (staging / name).write_text(text, encoding="utf-8")
            if target.exists():
                if not force:
                    raise OutputExistsError(f"{target} already exists; pass --force")
                target.rename(backup)
            staging.rename(target)
        except OSError as exc:
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise FatherAgentError(f"could not write {target}: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
        logger.info("wrote %s", target)
        return {name: target / name for name in texts}

    async def aclose(self) -> None:
        """Close provider connections."""
        await self.chain.aclose()


def load_spec(path: Path) -> SubAgentSpec:
    """Load a spec.json (or a sub-agent folder containing one)."""
    file = path / "spec.json" if path.is_dir() else path
    try:
        return SubAgentSpec.model_validate_json(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"cannot read {file}: {exc}") from exc
    except ValueError as exc:
        raise SpecError(f"{file} is not a valid spec: {exc}") from exc
