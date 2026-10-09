"""Capability detection: does the factory have what this task needs?

Runs before generation, offline, from the task text and the planned spec:

* a catalogue entry whose keywords appear in the task, and which no installed
  pack provides, is a gap;
* a planned third-party dependency that no installed pack provides is a gap,
  matched to the catalogue entry that ships it when there is one.

Nothing is written and nothing leaves the machine.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from ..spec import SubAgentSpec, import_name_for
from ..validator import check_licences
from .catalogue import CatalogueEntry, Pack, load_catalogue, load_registry
from .paths import EvolutionPaths


@dataclass
class Gap:
    """One thing the task needs that no installed pack provides."""

    need: str
    reasons: list[str]
    entry: CatalogueEntry | None = None
    licence_problems: list[str] = field(default_factory=list)

    @property
    def proposable(self) -> bool:
        """True when a curated entry covers it and passes the licence check."""
        return self.entry is not None and not self.licence_problems


@dataclass
class Detection:
    """The result of one capability check."""

    task: str
    installed: list[Pack]
    gaps: list[Gap] = field(default_factory=list)

    @property
    def found(self) -> bool:
        """True when at least one gap was found."""
        return bool(self.gaps)

    @property
    def have(self) -> list[str]:
        """Names of the installed packs."""
        return [p.name for p in self.installed]


def licence_problems(entry: CatalogueEntry) -> list[str]:
    """Why the validator's licence check rejects ``entry`` (empty when it passes)."""
    result = check_licences([(d.package, d.licence) for d in entry.dependencies],
                            needs_account=entry.needs_account)
    return [line for line in result.detail.splitlines() if line.strip()]


def detect(task: str, spec: SubAgentSpec | None = None,
           paths: EvolutionPaths | None = None) -> Detection:
    """Check ``task`` (and its planned ``spec``) against the registry and the catalogue."""
    paths = paths or EvolutionPaths()
    installed = load_registry(paths)
    catalogue = load_catalogue(paths)
    installed_names = {p.name for p in installed}
    provided = {import_name_for(pkg) for p in installed for pkg in p.provides}
    detection = Detection(task, installed)
    by_name: dict[str, Gap] = {}

    def gap_for(entry: CatalogueEntry, reason: str) -> None:
        gap = by_name.get(entry.name)
        if gap is None:
            gap = Gap(entry.need, [], entry, licence_problems(entry))
            by_name[entry.name] = gap
            detection.gaps.append(gap)
        if reason not in gap.reasons:
            gap.reasons.append(reason)

    for entry in catalogue:
        if entry.name in installed_names:
            continue
        hits = entry.matches(task)
        if hits and not all(import_name_for(d.package) in provided for d in entry.dependencies):
            gap_for(entry, "task mentions " + ", ".join(f'"{h}"' for h in hits))

    for dep in spec.dependencies if spec is not None else ():
        name = dep.import_name or import_name_for(dep.package)
        if name in provided or name in sys.stdlib_module_names:
            continue
        entry = next((e for e in catalogue if e.name not in installed_names and any(
            import_name_for(d.package) == name for d in e.dependencies)), None)
        if entry is not None:
            gap_for(entry, f"planned dependency {dep.package}")
        else:
            detection.gaps.append(Gap(dep.package, [f"planned dependency {dep.package}"]))
    return detection
