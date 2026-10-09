"""The registry (packs the factory ships) and the catalogue (packs it may propose).

Both are local JSON files read from disk. Nothing here, or anywhere in the
evolution module, makes a network call: the catalogue is curated by hand and
reviewed like code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import FatherAgentError
from .paths import EvolutionPaths


class EvolutionError(FatherAgentError):
    """The evolution module refused or could not do something."""


@dataclass(frozen=True)
class PackDependency:
    """One pip dependency of a catalogue entry, with the licence that admits it."""

    package: str
    version: str = ""
    licence: str = ""

    @property
    def requirement(self) -> str:
        """``kafka-python>=2.0,<3``."""
        return f"{self.package}{self.version}"

    def to_dict(self) -> dict[str, str]:
        """Plain dict for JSON."""
        return {"package": self.package, "version": self.version, "licence": self.licence}


@dataclass(frozen=True)
class Pack:
    """A capability pack listed in the registry (installed)."""

    name: str
    summary: str
    provides: tuple[str, ...]
    licence: str
    keywords: tuple[str, ...] = ()
    source: str = "builtin"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pack:
        """Build from a registry entry."""
        return cls(name=str(data["name"]), summary=str(data.get("summary", "")),
                   provides=tuple(data.get("provides", ())), licence=str(data.get("licence", "")),
                   keywords=tuple(data.get("keywords", ())), source=str(data.get("source", "")))


@dataclass(frozen=True)
class CatalogueEntry:
    """A pack the module may propose, if its licences pass the allowlist."""

    name: str
    need: str
    summary: str
    why: str
    keywords: tuple[str, ...]
    dependencies: tuple[PackDependency, ...]
    templates: tuple[str, ...] = ()
    needs_account: bool = False
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CatalogueEntry:
        """Build from a catalogue entry."""
        return cls(name=str(data["name"]), need=str(data.get("need", data["name"])),
                   summary=str(data.get("summary", "")), why=str(data.get("why", "")),
                   keywords=tuple(data.get("keywords", ())),
                   dependencies=tuple(PackDependency(**d) for d in data.get("dependencies", ())),
                   templates=tuple(data.get("templates", ())),
                   needs_account=bool(data.get("needs_account", False)))

    def matches(self, text: str) -> list[str]:
        """The keywords of this entry found in ``text`` (whole words, any case)."""
        return [kw for kw in self.keywords if keyword_in(kw, text)]


def keyword_in(keyword: str, text: str) -> bool:
    """True when ``keyword`` appears in ``text`` as whole words."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])",
                     text.lower()) is not None


def _read_json(path: Path) -> dict[str, Any]:
    """Parse one JSON file, with a clear error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvolutionError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvolutionError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EvolutionError(f"{path} must hold a JSON object")
    return data


def load_registry(paths: EvolutionPaths | None = None) -> list[Pack]:
    """Every installed pack, in registry order."""
    paths = paths or EvolutionPaths()
    return [Pack.from_dict(p) for p in _read_json(paths.registry).get("packs", [])]


def load_catalogue(paths: EvolutionPaths | None = None) -> list[CatalogueEntry]:
    """Every curated catalogue entry, proposable or not."""
    paths = paths or EvolutionPaths()
    return [CatalogueEntry.from_dict(e) for e in _read_json(paths.catalogue).get("entries", [])]


def registry_text_with(paths: EvolutionPaths, entry: dict[str, Any]) -> str:
    """The registry file with ``entry`` appended (existing entries untouched)."""
    data = _read_json(paths.registry)
    packs = list(data.get("packs", []))
    if any(p.get("name") == entry["name"] for p in packs):
        raise EvolutionError(f"pack {entry['name']!r} is already in the registry")
    data["packs"] = [*packs, entry]
    return json.dumps(data, indent=2) + "\n"
