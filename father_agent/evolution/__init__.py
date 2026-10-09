"""Self-evolution: notice a missing capability and propose an additive upgrade.

The factory checks each task against a local registry of capability packs
before generating anything. When a task needs something no pack provides, it
proposes one from a curated, offline catalogue: what it adds, why, every
dependency with its licence, the files it would write and the core it will
not touch. It applies the upgrade only after an interactive ``y``; no
terminal, or ``CI=true``, means declined.

Guard rails, all enforced by code:

* core files are pinned by sha256 (:mod:`.manifest`); the applier refuses them;
* only ``packs/<name>/``, a registry entry, ``requirements-packs.txt`` and the
  ledger are ever written (:mod:`.applier`);
* dependencies must carry an allowlisted open-source licence (validator check
  ``licence``);
* planning never makes a network call and never uses a hosted model.
"""

from .applier import CoreWriteRefused, apply
from .catalogue import EvolutionError, load_catalogue, load_registry
from .detector import Detection, Gap, detect
from .evolve import capability_lines, evolve, ledger_lines, planned_spec
from .gate import Decision, ask
from .manifest import CoreManifest, build_manifest, write_manifest
from .paths import EvolutionPaths
from .proposal import Proposal, build_proposal

__all__ = ["CoreManifest", "CoreWriteRefused", "Decision", "Detection", "EvolutionError",
           "EvolutionPaths", "Gap", "Proposal", "apply", "ask", "build_manifest",
           "build_proposal", "capability_lines", "detect", "evolve", "ledger_lines",
           "load_catalogue", "load_registry", "planned_spec", "write_manifest"]
