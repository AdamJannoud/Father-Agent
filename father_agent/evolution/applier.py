"""The applier: writes an accepted upgrade, additively, and never into the core.

The rules are enforced here, by code, not by the care of whoever calls it:

1. The core must match its pinned sha256 manifest before anything is written.
2. Every target path is checked first: a path named in the core manifest is
   refused, and so is any path outside the additive area (``packs/<name>/``,
   ``registry.json``, ``requirements-packs.txt``). Absolute paths, ``..`` and
   symlinks that lead elsewhere are refused too. One refusal stops the whole
   upgrade before a byte is written.
3. The pack's templates and licences go through the validator gate.
4. After writing, every core hash is checked again; if anything moved, the
   upgrade is rolled back and refused.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..validator import FileReport, Validator, gate_summary
from .catalogue import EvolutionError, registry_text_with
from .manifest import CoreManifest, ManifestCheck
from .paths import PACK_REQUIREMENTS, PACKS_DIR, REGISTRY_FILE, EvolutionPaths
from .proposal import Proposal


class CoreWriteRefused(EvolutionError):
    """A write targeted the core, or a path outside the additive area."""


@dataclass
class ApplyResult:
    """What an applied upgrade wrote and how the gate saw it."""

    written: list[str] = field(default_factory=list)
    reports: list[FileReport] = field(default_factory=list)
    manifest: ManifestCheck | None = None

    @property
    def gate(self) -> str:
        """One-line gate summary."""
        return gate_summary(self.reports)


def check_target(rel: str, manifest: CoreManifest, pack: str) -> str:
    """Return the normalised path, or raise :class:`CoreWriteRefused`."""
    path = PurePosixPath(rel.replace("\\", "/"))
    norm = path.as_posix()
    if path.is_absolute() or ".." in path.parts or norm in ("", "."):
        raise CoreWriteRefused(f"refused: {rel!r} is not a plain relative path")
    if norm in manifest:
        raise CoreWriteRefused(f"refused: {norm} is a core file (pinned by sha256 in the core "
                               f"manifest); an upgrade may never write it")
    resolved = (manifest.paths.root / norm).resolve()
    root = manifest.paths.root.resolve()
    if not resolved.is_relative_to(root):
        raise CoreWriteRefused(f"refused: {norm} resolves outside the repository")
    real = resolved.relative_to(root).as_posix()
    if real in manifest:
        raise CoreWriteRefused(f"refused: {norm} leads to the core file {real}")
    allowed_dir = f"{PACKS_DIR}/{pack}/"
    if not (real.startswith(allowed_dir) or real in (REGISTRY_FILE, PACK_REQUIREMENTS)):
        raise CoreWriteRefused(f"refused: {norm} is outside the additive area "
                               f"({allowed_dir}**, {REGISTRY_FILE}, {PACK_REQUIREMENTS})")
    return norm


async def apply(proposal: Proposal, paths: EvolutionPaths | None = None, *,
                validator: Validator | None = None) -> ApplyResult:
    """Write ``proposal``; raise :class:`EvolutionError` (nothing written) on any refusal."""
    paths = paths or EvolutionPaths()
    manifest = CoreManifest(paths)
    before = manifest.verify()
    if not before.intact:
        raise EvolutionError(f"the core does not match its manifest ({before.describe()}); "
                             "refusing to apply. If you changed a core file on purpose, review "
                             "it and run: python -m father_agent.evolution --write")
    if not proposal.files:
        raise EvolutionError("the proposal adds no files")
    if (paths.root / proposal.pack_dir).exists():
        raise EvolutionError(f"{proposal.pack_dir} already exists")

    # Every target is checked before anything is written.
    targets = {check_target(rel, manifest, proposal.name): text
               for rel, text in proposal.files.items()}
    for rel in (REGISTRY_FILE, PACK_REQUIREMENTS):
        check_target(rel, manifest, proposal.name)

    validator = validator or Validator()
    reports = await validator.validate_pack(
        proposal.templates(), [(d.package, d.licence) for d in proposal.entry.dependencies],
        needs_account=proposal.entry.needs_account)
    failed = [f"{r.filename}: {p}" for r in reports for p in r.problems]
    if failed:
        raise EvolutionError("the pack failed the gate; nothing was written:\n  - "
                             + "\n  - ".join(failed))

    registry_before = paths.registry.read_text(encoding="utf-8")
    requirements_before = (paths.pack_requirements.read_text(encoding="utf-8")
                           if paths.pack_requirements.is_file() else None)
    registry_after = registry_text_with(paths, proposal.registry_entry)
    result = ApplyResult(reports=reports)
    try:
        for rel, text in targets.items():
            target = paths.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            result.written.append(rel)
        paths.registry.write_text(registry_after, encoding="utf-8")
        result.written.append(REGISTRY_FILE)
        with paths.pack_requirements.open("a", encoding="utf-8") as handle:
            if requirements_before and not requirements_before.endswith("\n"):
                handle.write("\n")
            handle.write("\n".join(proposal.requirements) + "\n")
        result.written.append(PACK_REQUIREMENTS)
        result.manifest = manifest.verify()
        if not result.manifest.intact:
            raise CoreWriteRefused(f"the core changed while applying "
                                   f"({result.manifest.describe()}); rolled back")
    except BaseException:
        _rollback(paths, proposal, registry_before, requirements_before)
        raise
    return result


def _rollback(paths: EvolutionPaths, proposal: Proposal, registry: str,
              requirements: str | None) -> None:
    """Undo a partial apply: remove the pack folder, restore the two appended files."""
    shutil.rmtree(paths.root / proposal.pack_dir, ignore_errors=True)
    paths.registry.write_text(registry, encoding="utf-8")
    if requirements is None:
        Path(paths.pack_requirements).unlink(missing_ok=True)
    else:
        paths.pack_requirements.write_text(requirements, encoding="utf-8")
