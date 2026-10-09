"""The self-evolution loop: detect, propose, ask, apply (or not), record.

    task text + planned spec → detect a gap (registry, offline) → proposal
    printed → consent gate [y/N] → apply, additive only → ledger line

Nothing is written before the gate except the ledger line, and nothing is
ever written into a core file. Planning is offline: the spec comes from the
deterministic local planner, never from a hosted model.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import TextIO

from ..delivery import apply_delivery
from ..heuristics import plan_spec
from ..spec import SubAgentSpec
from ..validator import Validator, gate_summary
from . import gate, ledger
from .applier import apply
from .catalogue import EvolutionError, load_catalogue, load_registry
from .detector import Detection, Gap, detect, licence_problems
from .manifest import CoreManifest
from .paths import PACK_REQUIREMENTS, EvolutionPaths
from .proposal import Proposal, build_proposal

logger = logging.getLogger(__name__)


def planned_spec(task: str) -> SubAgentSpec:
    """The spec the offline planner would write for ``task`` (no network, no model)."""
    spec = SubAgentSpec.model_validate(plan_spec(task))
    return apply_delivery(spec)


def capability_lines(paths: EvolutionPaths | None = None) -> list[str]:
    """``main.py capabilities``: installed packs, proposable entries, rejected entries."""
    paths = paths or EvolutionPaths()
    installed = load_registry(paths)
    names = {p.name for p in installed}
    width = max([14, *(len(p.licence) + 2 for p in installed)])
    lines = [f"installed capability packs ({len(installed)})"]
    for pack in installed:
        lines.append(f"  {pack.name:<12}{', '.join(pack.provides):<34}{pack.licence:<{width}}"
                     f"{pack.summary}")
    proposable, rejected = [], []
    for entry in load_catalogue(paths):
        if entry.name in names:
            continue
        problems = licence_problems(entry)
        deps = ", ".join(d.package for d in entry.dependencies)
        licences = " / ".join(sorted({d.licence for d in entry.dependencies}))
        if problems:
            rejected.append(f"  {entry.name:<12}{deps:<34}{licences:<{width}}{problems[0]}"
                            " — never proposed")
        else:
            proposable.append(f"  {entry.name:<12}{deps:<34}{licences:<{width}}{entry.summary}")
    lines.append(f"proposable from the catalogue, not installed ({len(proposable)})")
    lines.extend(proposable or ["  (none)"])
    lines.append(f"rejected by the licence allowlist ({len(rejected)})")
    lines.extend(rejected or ["  (none)"])
    lines.append("the catalogue is local (father_agent/evolution/catalogue.json); "
                 "nothing is fetched")
    return lines


def ledger_lines(paths: EvolutionPaths | None = None) -> list[str]:
    """``main.py evolve --log``: one line per recorded proposal."""
    paths = paths or EvolutionPaths()
    rows = ledger.read(paths)
    if not rows:
        return [f"ledger is empty ({paths.ledger})"]
    lines = [f"ledger ({len(rows)} entries, {paths.ledger})"]
    for row in rows:
        lines.append(f"  {row.get('ts', '?'):<22}{row.get('decision', '?'):<10}"
                     f"{row.get('pack', '?'):<12}{', '.join(row.get('dependencies', [])):<28}"
                     f"{row.get('task', '')}")
        if row.get("reason"):
            lines.append(f"  {'':<22}reason: {row['reason']} · {row.get('diff_hash', '')[:19]}")
    return lines


def _gap_lines(detection: Detection) -> list[str]:
    lines = ["capability check … gap found"]
    for gap in detection.gaps:
        lines.append(f"  need : {gap.need}  ({'; '.join(gap.reasons)})")
        if gap.entry is None:
            lines.append("         no curated pack provides it — nothing to propose")
        elif gap.licence_problems:
            lines.append(f"         rejected by the licence allowlist: {gap.licence_problems[0]}"
                         " — never proposed")
    lines.append(f"  have : {' · '.join(detection.have)}")
    return lines


def _proposal_lines(proposal: Proposal, gate_line: str, manifest_count: int) -> list[str]:
    files = [p.rsplit("/", 1)[-1] if p.endswith("pack.json") else p.split("/", 4)[-1]
             for p in proposal.files]
    deps = [f"{d.package} {d.version or '(any)'} · {d.licence} · open-source, no account, "
            f"no key" for d in proposal.entry.dependencies]
    lines = ["proposed upgrade — nothing written yet",
             f"  add    capability pack \"{proposal.name}\"",
             f"         {' + '.join(files)}",
             f"  why    {proposal.entry.why}",
             f"         ({'; '.join(proposal.reasons)})"]
    lines += [f"  {'deps' if i == 0 else '':<7}{d}" for i, d in enumerate(deps)] or \
        ["  deps   none"]
    lines += [f"  writes {proposal.pack_dir}/** · registry entry · {PACK_REQUIREMENTS} · ledger",
              f"  never  core modules or requirements.txt — manifest {manifest_count} files, "
              f"sha256 pinned",
              f"  gate   {gate_line} (checked in memory)",
              f"  diff   {proposal.diff_hash}"]
    return lines


async def evolve(task: str, *, spec: SubAgentSpec | None = None,
                 paths: EvolutionPaths | None = None, assume_yes: bool = False,
                 stdin: TextIO | None = None, stdout: TextIO | None = None,
                 env: Mapping[str, str] | None = None, report_covered: bool = True,
                 validator: Validator | None = None) -> int:
    """Run the loop once for ``task``. Returns 0 (no gap, applied or declined) or 1."""
    paths = paths or EvolutionPaths()
    stdout = stdout or sys.stdout

    def say(line: str = "") -> None:
        stdout.write(line + "\n")

    detection = detect(task, spec, paths)
    if not detection.found:
        if report_covered:
            say(f"capability check … no gap (installed: {' · '.join(detection.have)})")
        return 0
    for line in _gap_lines(detection):
        say(line)
    proposable: list[Gap] = [g for g in detection.gaps if g.proposable]
    if not proposable:
        say("nothing to propose: the module only adds curated, open-source packs")
        return 0

    manifest = CoreManifest(paths)
    validator = validator or Validator()
    code = 0
    for gap in proposable:
        proposal = build_proposal(gap, task, paths)
        reports = await validator.validate_pack(
            proposal.templates(), [(d.package, d.licence) for d in proposal.entry.dependencies],
            needs_account=proposal.entry.needs_account)
        for line in _proposal_lines(proposal, gate_summary(reports), len(manifest)):
            say(line)
        snapshot = manifest.snapshot()
        decision = gate.ask("apply this upgrade?", assume_yes=assume_yes, stdin=stdin,
                            stdout=stdout, env=env)
        outcome, reason = ("applied", decision.reason) if decision.approved else \
            ("declined", decision.reason)
        if decision.approved:
            try:
                result = await apply(proposal, paths, validator=validator)
            except EvolutionError as exc:
                outcome, reason, code = "refused", str(exc).splitlines()[0], 1
                say(f"  refused — nothing written: {exc}")
            else:
                say(f"  applied — wrote {len(result.written)} files: "
                    + ", ".join(result.written))
                say(f"  gate   {result.gate}")
        elif decision.reason == "no terminal to confirm on" or decision.reason.startswith("CI"):
            say(f"declined: {decision.reason}")
            say("  re-run interactively, or pass --yes, to apply"
                if not decision.reason.startswith("CI") else
                "  run it on your own machine, interactively, to apply")
        else:
            say(f"  declined — nothing written ({decision.reason}).")
        check = manifest.verify()
        moved = manifest.snapshot() != snapshot
        say(f"core manifest {'intact' if check.intact else 'NOT intact'} ({check.describe()})"
            + ("" if not moved else " — CHANGED during this run"))
        line = ledger.record(paths, task=task, pack=proposal.name,
                             dependencies=proposal.dependencies, decision=outcome,
                             reason=reason, diff_hash=proposal.diff_hash)
        say(f"ledger ← {json.dumps({k: line[k] for k in ('decision', 'pack', 'ts', 'task')})}")
        if outcome == "applied":
            say(f"next: pip install -r {PACK_REQUIREMENTS}  ·  bash scripts/verify.sh")
    return code
