"""The state a fresh clone ships: a miniature checkout built from it, never borrowed.

An accepted upgrade rewrites ``registry.json`` and adds a pack folder by design,
so a checkout that has been evolved is no longer the checkout the factory ships:
a task that was a gap there is covered here. Anything that exercises the loop
from the shipped state (the test fixture, the smoke check in
``scripts/verify.sh``) must build that state explicitly, and both call this
module so they cannot drift apart.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from .manifest import write_manifest
from .paths import CATALOGUE_FILE, PACKS_DIR, REGISTRY_FILE, TEMPLATES_DIR

#: The ``source`` of registry entries the factory ships (accepted packs say ``pack:<name>``).
SHIPPED_SOURCE = "builtin"
#: Core files a miniature checkout needs for the manifest and the core guard.
CORE_FILES = (
    "main.py", "requirements.txt", "requirements-packs.txt", "father_agent/cli.py",
    "father_agent/evolution/applier.py",
)
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def shipped_packs(registry: dict[str, Any]) -> list[dict[str, Any]]:
    """The entries of ``registry["packs"]`` the factory ships, in order."""
    return [p for p in registry.get("packs", []) if p.get("source") == SHIPPED_SOURCE]


def write_pristine_checkout(dest: Path, *, root: Path = PROJECT_ROOT) -> Path:
    """Build a miniature checkout of the shipped state of ``root`` under ``dest``."""
    shutil.copy2(root / CATALOGUE_FILE, _parent(dest / CATALOGUE_FILE))
    shutil.copytree(root / TEMPLATES_DIR, dest / TEMPLATES_DIR, ignore=_IGNORE)

    registry = json.loads((root / REGISTRY_FILE).read_text(encoding="utf-8"))
    registry["packs"] = shipped = shipped_packs(registry)
    _parent(dest / REGISTRY_FILE).write_text(json.dumps(registry, indent=2) + "\n",
                                             encoding="utf-8")

    names = {p.get("name") for p in shipped}
    (dest / PACKS_DIR).mkdir(parents=True, exist_ok=True)
    for src in sorted((root / PACKS_DIR).iterdir()):
        if src.is_file() and src.suffix != ".pyc":
            shutil.copy2(src, dest / PACKS_DIR / src.name)
        elif src.is_dir() and src.name in names:
            shutil.copytree(src, dest / PACKS_DIR / src.name, ignore=_IGNORE)

    for rel in CORE_FILES:
        shutil.copy2(root / rel, _parent(dest / rel))
    write_manifest(dest)
    return dest


def _parent(path: Path) -> Path:
    """Create ``path``'s parent folder; returns ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
