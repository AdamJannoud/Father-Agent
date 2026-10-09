"""Self-evolution: detection before generation, the fail-closed gate, the core guard,
the licence allowlist and the ledger. Every test runs offline (conftest blocks sockets)
and writes only into a throw-away copy of the evolution files."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path

import pytest

from father_agent.cli import main
from father_agent.evolution import (
    CoreManifest,
    CoreWriteRefused,
    EvolutionError,
    EvolutionPaths,
    apply,
    ask,
    build_manifest,
    build_proposal,
    detect,
    evolve,
    planned_spec,
    write_manifest,
)
from father_agent.evolution.applier import check_target
from father_agent.evolution.paths import (
    EVOLUTION_DIR,
    LEDGER_FILE,
    MANIFEST_FILE,
    PACK_REQUIREMENTS,
    REGISTRY_FILE,
)
from father_agent.spec import Dependency
from father_agent.validator import Validator, check_licences, licence_allowed

from .conftest import ROOT, SAMPLE_COMMAND

KAFKA_TASK = "watch a kafka topic and alert on spikes"
#: The commands scripts/verify.sh generates: none of them may report a gap.
COVERED_TASKS = (
    SAMPLE_COMMAND,
    "a Telegram bot that watches a Solana wallet and DMs me on changes",
    "a dashboard that tracks Solana priority fees and plots the last hour",
    "track the bitcoin price API every 5 minutes",
    "a service that exposes the bitcoin price over an HTTP API",
    "scrape the headlines of a website every hour",
    "train a classifier on a csv dataset daily",
)


class FakeTTY(io.StringIO):
    """A stdin that claims to be an interactive terminal, with scripted answers."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A miniature checkout: the real evolution files plus a few core files, pinned."""
    fake = tmp_path / "repo"
    (fake / EVOLUTION_DIR).mkdir(parents=True)
    for name in ("registry.json", "catalogue.json", "templates", "packs"):
        src = ROOT / EVOLUTION_DIR / name
        if src.is_dir():
            shutil.copytree(src, fake / EVOLUTION_DIR / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, fake / EVOLUTION_DIR / name)
    for rel in ("main.py", "requirements.txt", "father_agent/cli.py",
                "father_agent/evolution/applier.py", PACK_REQUIREMENTS):
        (fake / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, fake / rel)
    write_manifest(fake)
    return fake


def snapshot(folder: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Every file under ``folder`` and its bytes."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*"))
            if p.is_file() and p.relative_to(folder).as_posix() not in skip}


def run_evolve(root: Path, *, stdin: io.StringIO, env: dict[str, str] | None = None,
               assume_yes: bool = False, task: str = KAFKA_TASK) -> tuple[int, str]:
    """Run the loop once against ``root``; returns (exit code, output)."""
    out = io.StringIO()
    code = asyncio.run(evolve(task, spec=planned_spec(task), paths=EvolutionPaths(root),
                              assume_yes=assume_yes, stdin=stdin, stdout=out,
                              env={} if env is None else env))
    return code, out.getvalue()


def ledger_rows(root: Path) -> list[dict[str, object]]:
    path = root / LEDGER_FILE
    return [json.loads(x) for x in path.read_text().splitlines()] if path.is_file() else []


# --------------------------------------------------------------------------- detection


def test_gap_is_detected_before_any_file_is_written(root: Path) -> None:
    before = snapshot(root)
    detection = detect(KAFKA_TASK, planned_spec(KAFKA_TASK), EvolutionPaths(root))
    assert detection.found
    gap = detection.gaps[0]
    assert gap.entry is not None and gap.entry.name == "kafka" and gap.proposable
    assert "kafka" in gap.need and any('"kafka"' in r for r in gap.reasons)
    proposal = build_proposal(gap, KAFKA_TASK, EvolutionPaths(root))
    assert proposal.dependencies == ["kafka-python>=2.0,<3"]
    assert set(proposal.files) == {f"{EVOLUTION_DIR}/packs/kafka/templates/consumer.py",
                                   f"{EVOLUTION_DIR}/packs/kafka/pack.json"}
    assert snapshot(root) == before, "detection and proposal must not touch the disk"


@pytest.mark.parametrize("task", COVERED_TASKS)
def test_tasks_the_factory_covers_report_no_gap(task: str) -> None:
    assert not detect(task, planned_spec(task)).found


def test_planned_dependency_without_a_pack_is_a_gap(root: Path) -> None:
    spec = planned_spec("track the bitcoin price API every 5 minutes")
    spec.dependencies.append(Dependency(package="networkx", purpose="graph"))
    spec.dependencies.append(Dependency(package="mystery-lib", purpose="?"))
    gaps = {g.need: g for g in detect(spec.command, spec, EvolutionPaths(root)).gaps}
    graph = next(g for g in gaps.values() if g.entry is not None)
    assert graph.entry.name == "graph" and graph.proposable
    assert gaps["mystery-lib"].entry is None and not gaps["mystery-lib"].proposable


# --------------------------------------------------------------------------- the gate


def test_gate_declines_when_there_is_no_tty() -> None:
    decision = ask("apply?", stdin=io.StringIO("y\n"), stdout=io.StringIO(), env={})
    assert not decision.approved and decision.reason == "no terminal to confirm on"


def test_yes_still_declines_under_ci() -> None:
    for value in ("true", "1", "TRUE"):
        decision = ask("apply?", assume_yes=True, stdin=FakeTTY("y\n"),
                       stdout=io.StringIO(), env={"CI": value})
        assert not decision.approved and decision.reason.startswith("CI=true")


@pytest.mark.parametrize("answer", ["", "\n", "n\n", "no\n", "maybe\n", "Y es\n"])
def test_gate_defaults_to_no(answer: str) -> None:
    assert not ask("apply?", stdin=FakeTTY(answer), stdout=io.StringIO(), env={}).approved


def test_gate_approves_only_an_explicit_yes() -> None:
    out = io.StringIO()
    assert ask("apply?", stdin=FakeTTY("y\n"), stdout=out, env={}).approved
    assert out.getvalue() == "apply? [y/N] "
    assert ask("apply?", assume_yes=True, stdin=io.StringIO(), stdout=out, env={}).approved


def test_declined_proposal_writes_nothing(root: Path) -> None:
    before = snapshot(root)
    code, text = run_evolve(root, stdin=FakeTTY("n\n"))
    assert code == 0
    assert "proposed upgrade — nothing written yet" in text
    assert "kafka-python >=2.0,<3 · Apache-2.0" in text
    assert "declined — nothing written" in text and "core manifest intact" in text
    assert snapshot(root, skip=(LEDGER_FILE,)) == before
    assert not (root / EVOLUTION_DIR / "packs" / "kafka").exists()
    [row] = ledger_rows(root)
    assert row["decision"] == "declined" and row["pack"] == "kafka"
    assert row["task"] == KAFKA_TASK and str(row["diff_hash"]).startswith("sha256:")


def test_no_tty_run_declines_and_says_how_to_apply(root: Path) -> None:
    before = snapshot(root)
    code, text = run_evolve(root, stdin=io.StringIO("y\n"))
    assert code == 0
    assert "declined: no terminal to confirm on" in text and "pass --yes" in text
    assert snapshot(root, skip=(LEDGER_FILE,)) == before


def test_yes_under_ci_writes_nothing(root: Path) -> None:
    before = snapshot(root)
    code, text = run_evolve(root, stdin=FakeTTY("y\n"), env={"CI": "true"}, assume_yes=True)
    assert code == 0 and "declined: CI=true" in text
    assert snapshot(root, skip=(LEDGER_FILE,)) == before
    assert ledger_rows(root)[0]["decision"] == "declined"


# --------------------------------------------------------------------------- applying


def test_yes_at_the_terminal_applies_additively(root: Path) -> None:
    core_before = CoreManifest(EvolutionPaths(root)).snapshot()
    registry_before = json.loads((root / REGISTRY_FILE).read_text())["packs"]
    code, text = run_evolve(root, stdin=FakeTTY("y\n"))
    assert code == 0 and "applied — wrote" in text, text
    pack = root / EVOLUTION_DIR / "packs" / "kafka"
    assert (pack / "templates" / "consumer.py").is_file()
    assert json.loads((pack / "pack.json").read_text())["dependencies"][0]["licence"] \
        == "Apache-2.0"
    registry = json.loads((root / REGISTRY_FILE).read_text())["packs"]
    assert registry[:-1] == registry_before and registry[-1]["name"] == "kafka"
    assert "kafka-python>=2.0,<3" in (root / PACK_REQUIREMENTS).read_text()
    assert (root / "requirements.txt").read_bytes() == (ROOT / "requirements.txt").read_bytes()
    assert CoreManifest(EvolutionPaths(root)).snapshot() == core_before
    assert ledger_rows(root)[-1]["decision"] == "applied"
    # The capability is now installed: the same task finds no gap.
    assert not detect(KAFKA_TASK, planned_spec(KAFKA_TASK), EvolutionPaths(root)).found


# --------------------------------------------------------------------------- the guard


@pytest.mark.parametrize("target", [
    "father_agent/cli.py",                      # a core module
    "father_agent/evolution/applier.py",        # the guard itself
    "requirements.txt",                         # the core requirements file
    "main.py",
    MANIFEST_FILE,                              # the manifest
])
def test_applier_refuses_a_core_path(root: Path, target: str) -> None:
    """The point of the design: a core-file write is refused by code, not by care."""
    paths = EvolutionPaths(root)
    gap = detect(KAFKA_TASK, None, paths).gaps[0]
    proposal = build_proposal(gap, KAFKA_TASK, paths)
    proposal.files[target] = "# overwritten by an upgrade\n"
    before = snapshot(root)
    with pytest.raises(CoreWriteRefused, match="core"):
        asyncio.run(apply(proposal, paths, validator=Validator(use_ruff=False)))
    assert snapshot(root) == before, "a refused upgrade must write nothing at all"


@pytest.mark.parametrize("target", [
    "../outside.py", "/etc/evil.py", "father_agent/evolution/packs/other/x.py",
    "father_agent/new_module.py", "tests/test_backdoor.py",
])
def test_applier_refuses_paths_outside_the_additive_area(root: Path, target: str) -> None:
    paths = EvolutionPaths(root)
    proposal = build_proposal(detect(KAFKA_TASK, None, paths).gaps[0], KAFKA_TASK, paths)
    proposal.files[target] = "x = 1\n"
    before = snapshot(root)
    with pytest.raises(CoreWriteRefused, match="refused"):
        asyncio.run(apply(proposal, paths, validator=Validator(use_ruff=False)))
    assert snapshot(root) == before


def test_applier_refuses_a_symlink_into_the_core(root: Path) -> None:
    paths = EvolutionPaths(root)
    shutil.rmtree(paths.packs)
    paths.packs.symlink_to(root / "father_agent", target_is_directory=True)
    proposal = build_proposal(detect(KAFKA_TASK, None, paths).gaps[0], KAFKA_TASK, paths)
    with pytest.raises(CoreWriteRefused, match="outside the additive area"):
        asyncio.run(apply(proposal, paths, validator=Validator(use_ruff=False)))
    assert not (root / "father_agent" / "kafka").exists()


def test_real_manifest_refuses_real_core_files() -> None:
    manifest = CoreManifest()
    for rel in ("father_agent/cli.py", "father_agent/evolution/applier.py", "requirements.txt",
                "scripts/verify.sh", ".github/workflows/ci.yml", MANIFEST_FILE):
        with pytest.raises(CoreWriteRefused):
            check_target(rel, manifest, "kafka")
    assert check_target(f"{EVOLUTION_DIR}/packs/kafka/pack.json", manifest, "kafka")


def test_applier_refuses_when_the_core_drifted(root: Path) -> None:
    paths = EvolutionPaths(root)
    proposal = build_proposal(detect(KAFKA_TASK, None, paths).gaps[0], KAFKA_TASK, paths)
    (root / "main.py").write_text("# edited\n")
    with pytest.raises(EvolutionError, match="does not match its manifest"):
        asyncio.run(apply(proposal, paths, validator=Validator(use_ruff=False)))
    assert not (paths.packs / "kafka").exists()


def test_committed_core_manifest_is_current() -> None:
    """Changed a core file on purpose? Run: python -m father_agent.evolution --write"""
    pinned = CoreManifest().files
    current = build_manifest(ROOT)
    assert sorted(current) == sorted(pinned), "core files added or removed"
    stale = [rel for rel in current if current[rel] != pinned[rel]]
    assert not stale, f"core manifest is stale for: {stale}"


# --------------------------------------------------------------------------- licences


def test_validator_rejects_a_licence_off_the_allowlist() -> None:
    for licence in ("GPL-3.0-only", "AGPL-3.0", "proprietary", "SSPL-1.0", ""):
        result = check_licences([("some-lib", licence)])
        assert not result.ok and result.name == "licence"
    for licence in ("MIT", "BSD-3-Clause", "apache-2.0", "MPL-2.0", "LGPL-3.0-or-later"):
        assert licence_allowed(licence)
        assert check_licences([("some-lib", licence)]).ok
    assert not check_licences([("openai", "MIT")]).ok, "paid API SDKs are never allowed"
    assert not check_licences([("free-lib", "MIT")], needs_account=True).ok


def test_validate_pack_reports_the_licence_check() -> None:
    reports = asyncio.run(Validator(use_ruff=False).validate_pack(
        {"x.py": '"""Doc."""\n'}, [("gpl-lib", "GPL-3.0-only")]))
    assert reports[-1].filename == "licence" and not reports[-1].ok
    assert "not on the open-source allowlist" in reports[-1].problems[0]


def test_off_allowlist_catalogue_entry_is_never_proposed(root: Path) -> None:
    catalogue = root / EVOLUTION_DIR / "catalogue.json"
    data = json.loads(catalogue.read_text())
    kafka = next(e for e in data["entries"] if e["name"] == "kafka")
    kafka["dependencies"][0]["licence"] = "GPL-3.0-only"
    catalogue.write_text(json.dumps(data))
    before = snapshot(root)
    code, text = run_evolve(root, stdin=FakeTTY("y\n"))
    assert code == 0
    assert "rejected by the licence allowlist" in text and "proposed upgrade" not in text
    assert snapshot(root) == before, "nothing proposed, so nothing written, not even a ledger line"


def test_evolution_code_has_no_network_client() -> None:
    for path in (ROOT / EVOLUTION_DIR).glob("*.py"):
        source = path.read_text()
        for needle in ("import httpx", "import urllib", "import requests", "import socket",
                       "providers"):
            assert needle not in source, f"{path.name} must stay offline ({needle})"


# --------------------------------------------------------------------------- CLI


def test_cli_capabilities_lists_packs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["capabilities"]) == 0
    text = capsys.readouterr().out
    assert "installed capability packs" in text and "http" in text and "httpx" in text
    assert "kafka" in text and "Apache-2.0" in text
    assert "priced-feed" in text and "never proposed" in text


def test_cli_evolve_without_a_terminal_declines(root: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("FATHER_EVOLUTION_ROOT", str(root))
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    before = snapshot(root)
    assert main(["evolve", KAFKA_TASK]) == 0
    text = capsys.readouterr().out
    assert "gap found" in text and "proposed upgrade" in text and "declined" in text
    assert snapshot(root, skip=(LEDGER_FILE,)) == before
    assert main(["evolve", "--log"]) == 0
    assert "declined" in capsys.readouterr().out and "kafka" in text
    assert main(["evolve"]) == 2


def test_cli_evolve_yes_under_ci_declines(root: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("FATHER_EVOLUTION_ROOT", str(root))
    monkeypatch.setenv("CI", "true")
    assert main(["evolve", "--yes", KAFKA_TASK]) == 0
    assert "declined: CI=true" in capsys.readouterr().out
    assert not (root / EVOLUTION_DIR / "packs" / "kafka").exists()


def test_cli_new_runs_the_same_check_after_generating(
        root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("FATHER_EVOLUTION_ROOT", str(root))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    out = tmp_path / "agents"
    assert main(["new", "-o", str(out), "a tracker that reads a kafka topic every 60s"]) == 0
    text = capsys.readouterr().out
    assert "gap found" in text and "capability pack \"kafka\"" in text and "declined" in text
    assert not (root / EVOLUTION_DIR / "packs" / "kafka").exists()


def test_cli_new_without_a_gap_prints_no_proposal(capsys: pytest.CaptureFixture[str],
                                                  tmp_path: Path) -> None:
    assert main(["new", "-o", str(tmp_path / "a"), SAMPLE_COMMAND]) == 0
    text = capsys.readouterr().out
    assert "capability check" not in text and "proposed upgrade" not in text


def test_help_lists_the_evolution_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    assert "capabilities" in text and "evolve" in text
