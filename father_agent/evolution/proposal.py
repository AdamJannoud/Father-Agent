"""A proposal: exactly what an upgrade would add, built in memory.

It names the pack, why the task needs it, every dependency with its licence,
every file it would write and the core it will not touch. Nothing is written
while a proposal is built or shown.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..errors import FatherAgentError
from .catalogue import CatalogueEntry, EvolutionError, registry_text_with
from .detector import Gap
from .paths import LEDGER_FILE, PACK_REQUIREMENTS, PACKS_DIR, REGISTRY_FILE, EvolutionPaths


@dataclass
class Proposal:
    """One additive upgrade, not yet applied."""

    task: str
    entry: CatalogueEntry
    reasons: list[str]
    #: ``relative path -> text`` of every new file under ``packs/<name>/``.
    files: dict[str, str] = field(default_factory=dict)
    registry_entry: dict[str, Any] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """The pack name."""
        return self.entry.name

    @property
    def pack_dir(self) -> str:
        """``father_agent/evolution/packs/<name>``."""
        return f"{PACKS_DIR}/{self.name}"

    @property
    def dependencies(self) -> list[str]:
        """Pinned requirement strings."""
        return [d.requirement for d in self.entry.dependencies]

    @property
    def writes(self) -> list[str]:
        """Every path the upgrade writes or appends to, in order."""
        return [*self.files, REGISTRY_FILE, PACK_REQUIREMENTS, LEDGER_FILE]

    @property
    def diff_hash(self) -> str:
        """sha256 over the exact content the upgrade would add."""
        blob = json.dumps({"files": self.files, "registry": self.registry_entry,
                           "requirements": self.requirements}, sort_keys=True)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def templates(self) -> dict[str, str]:
        """The Python template files, keyed by file name (for the gate)."""
        return {path.rsplit("/", 1)[-1]: text for path, text in self.files.items()
                if path.endswith(".py")}


def build_proposal(gap: Gap, task: str, paths: EvolutionPaths | None = None) -> Proposal:
    """Read the entry's templates from the local catalogue and assemble the upgrade."""
    paths = paths or EvolutionPaths()
    if gap.entry is None or not gap.proposable:
        raise EvolutionError(f"nothing may be proposed for {gap.need}")
    entry = gap.entry
    proposal = Proposal(task, entry, list(gap.reasons))
    for name in entry.templates:
        source = paths.templates / entry.name / name
        try:
            proposal.files[f"{proposal.pack_dir}/templates/{name}"] = \
                source.read_text(encoding="utf-8")
        except OSError as exc:
            raise EvolutionError(f"catalogue template {source} cannot be read: {exc}") from exc
    pack = {"name": entry.name, "need": entry.need, "summary": entry.summary, "why": entry.why,
            "dependencies": [d.to_dict() for d in entry.dependencies],
            "templates": [f"templates/{n}" for n in entry.templates],
            "added_for": task}
    proposal.files[f"{proposal.pack_dir}/pack.json"] = json.dumps(pack, indent=2) + "\n"
    proposal.registry_entry = {
        "name": entry.name, "summary": entry.summary,
        "provides": [d.package for d in entry.dependencies],
        "licence": " / ".join(sorted({d.licence for d in entry.dependencies})),
        "source": f"pack:{proposal.pack_dir}", "keywords": list(entry.keywords)}
    try:
        registry_text_with(paths, proposal.registry_entry)
    except FatherAgentError as exc:
        raise EvolutionError(str(exc)) from exc
    proposal.requirements = [f"{d.requirement}  # pack {entry.name} · {d.licence}"
                             for d in entry.dependencies]
    return proposal
