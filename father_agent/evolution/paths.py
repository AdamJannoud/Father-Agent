"""Where the self-evolution module reads and writes, relative to one root.

Everything an accepted upgrade may touch is listed here, and nothing else:
``packs/<name>/``, one entry appended to ``registry.json``, lines appended to
``requirements-packs.txt`` (never the core ``requirements.txt``) and one line
per proposal in ``ledger.jsonl``. Every other file named in the core manifest
is refused by :mod:`.applier`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import PROJECT_ROOT

#: The package folder, relative to the repository root.
EVOLUTION_DIR = "father_agent/evolution"
PACKS_DIR = f"{EVOLUTION_DIR}/packs"
REGISTRY_FILE = f"{EVOLUTION_DIR}/registry.json"
LEDGER_FILE = f"{EVOLUTION_DIR}/ledger.jsonl"
MANIFEST_FILE = f"{EVOLUTION_DIR}/core_manifest.json"
CATALOGUE_FILE = f"{EVOLUTION_DIR}/catalogue.json"
TEMPLATES_DIR = f"{EVOLUTION_DIR}/templates"
#: Optional dependencies of accepted packs. The core requirements.txt is never touched.
PACK_REQUIREMENTS = "requirements-packs.txt"


@dataclass(frozen=True)
class EvolutionPaths:
    """Absolute paths of the evolution files under one repository root."""

    root: Path = PROJECT_ROOT

    def path(self, rel: str) -> Path:
        """``root / rel``."""
        return self.root / rel

    @property
    def registry(self) -> Path:
        """The registry of installed capability packs."""
        return self.path(REGISTRY_FILE)

    @property
    def catalogue(self) -> Path:
        """The curated catalogue of packs that may be proposed."""
        return self.path(CATALOGUE_FILE)

    @property
    def templates(self) -> Path:
        """Template sources for catalogue entries (core, read-only)."""
        return self.path(TEMPLATES_DIR)

    @property
    def packs(self) -> Path:
        """Where accepted packs are written."""
        return self.path(PACKS_DIR)

    @property
    def ledger(self) -> Path:
        """One JSON line per proposal and decision."""
        return self.path(LEDGER_FILE)

    @property
    def manifest(self) -> Path:
        """sha256 of every core file."""
        return self.path(MANIFEST_FILE)

    @property
    def pack_requirements(self) -> Path:
        """Optional requirements of accepted packs."""
        return self.path(PACK_REQUIREMENTS)
