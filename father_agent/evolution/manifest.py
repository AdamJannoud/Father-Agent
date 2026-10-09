"""The core manifest: every core file of the factory, pinned by sha256.

The applier refuses to write any path named here, and re-checks every hash
after it applies an upgrade. Regenerate it after you change a core file on
purpose (the test suite fails until you do)::

    python -m father_agent.evolution --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .catalogue import EvolutionError
from .paths import MANIFEST_FILE, PACKS_DIR, EvolutionPaths

#: Globs (relative to the repository root) that make up the core.
CORE_GLOBS = (
    "main.py", "pyproject.toml", "requirements.txt", "requirements-dev.txt", "LICENSE",
    "README.md", ".gitignore", ".env.example", "scripts/*", "prompts/*",
    ".github/workflows/*", "father_agent/**/*.py", "father_agent/evolution/catalogue.json",
    "father_agent/evolution/templates/**/*", "tests/**/*.py",
)
#: Never part of the core: the additive area an accepted upgrade writes to.
_EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def _sha256(path: Path) -> str:
    """Hex sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def core_files(root: Path) -> list[str]:
    """Relative posix paths of every core file that exists under ``root``."""
    found: set[str] = set()
    for pattern in CORE_GLOBS:
        for path in root.glob(pattern):
            rel = path.relative_to(root).as_posix()
            if not path.is_file() or rel.startswith(PACKS_DIR + "/") \
                    or _EXCLUDED_PARTS & set(path.relative_to(root).parts) \
                    or path.suffix in (".pyc",):
                continue
            found.add(rel)
    return sorted(found)


def build_manifest(root: Path) -> dict[str, str]:
    """``{relative path: sha256}`` of the core under ``root`` as it is now."""
    return {rel: _sha256(root / rel) for rel in core_files(root)}


@dataclass
class ManifestCheck:
    """How the files on disk compare with the pinned hashes."""

    total: int
    changed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        """True when every pinned file exists with its pinned hash."""
        return not self.changed and not self.missing

    def describe(self) -> str:
        """``41 files, hashes unchanged`` or the files that differ."""
        if self.intact:
            return f"{self.total} files, hashes unchanged"
        parts = [f"changed: {', '.join(self.changed)}" if self.changed else "",
                 f"missing: {', '.join(self.missing)}" if self.missing else ""]
        return f"{self.total} files; " + "; ".join(p for p in parts if p)


class CoreManifest:
    """The pinned manifest of one repository root."""

    def __init__(self, paths: EvolutionPaths | None = None) -> None:
        """Load ``core_manifest.json``; a missing or unreadable manifest is an error."""
        self.paths = paths or EvolutionPaths()
        try:
            data = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
            self.files: dict[str, str] = dict(data["files"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EvolutionError(f"the core manifest {self.paths.manifest} cannot be read "
                                 f"({exc}); refusing to evolve without it") from exc

    def __contains__(self, rel: str) -> bool:
        """True when ``rel`` is a core path (the manifest itself counts as core)."""
        return rel in self.files or rel == MANIFEST_FILE

    def __len__(self) -> int:
        """Number of pinned files."""
        return len(self.files)

    def snapshot(self) -> dict[str, str | None]:
        """Current sha256 of every pinned file (None when it is missing)."""
        out: dict[str, str | None] = {}
        for rel in self.files:
            path = self.paths.root / rel
            out[rel] = _sha256(path) if path.is_file() else None
        return out

    def verify(self) -> ManifestCheck:
        """Compare the files on disk with the pinned hashes."""
        check = ManifestCheck(len(self.files))
        for rel, current in self.snapshot().items():
            if current is None:
                check.missing.append(rel)
            elif current != self.files[rel]:
                check.changed.append(rel)
        return check


def write_manifest(root: Path) -> Path:
    """Regenerate ``core_manifest.json`` under ``root``; returns its path."""
    paths = EvolutionPaths(root)
    data = {"about": "sha256 of every core file. The evolution applier refuses to write any "
                     "path listed here. Regenerate: python -m father_agent.evolution "
                     "--write",
            "files": build_manifest(root)}
    paths.manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return paths.manifest


def main(argv: list[str] | None = None) -> int:
    """``--check`` (default) or ``--write`` the manifest of this repository."""
    parser = argparse.ArgumentParser(prog="python -m father_agent.evolution")
    parser.add_argument("--write", action="store_true", help="regenerate the manifest")
    parser.add_argument("--root", type=Path, default=EvolutionPaths().root)
    args = parser.parse_args(argv)
    if args.write:
        path = write_manifest(args.root)
        sys.stdout.write(f"wrote {path} ({len(core_files(args.root))} core files)\n")
        return 0
    manifest = CoreManifest(EvolutionPaths(args.root))
    current = set(core_files(args.root))
    check = manifest.verify()
    unpinned = sorted(current - set(manifest.files))
    sys.stdout.write(f"core manifest: {check.describe()}"
                     + (f"; not pinned yet: {', '.join(unpinned)}" if unpinned else "") + "\n")
    return 0 if check.intact and not unpinned else 1


if __name__ == "__main__":
    raise SystemExit(main())
