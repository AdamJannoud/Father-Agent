"""Run the Render Blueprint's buildCommand verbatim and fail when it fails.

    python scripts/run_render_build.py

CI's `render-build` job runs this on Python 3.12, so it exercises the literal
command Render runs (services[0].buildCommand in render.yaml) rather than a copy
that can drift. It installs into whatever interpreter `pip` resolves to: run it
in a throwaway venv or a CI runner, not in your own environment.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def build_command(blueprint: Path = ROOT / "render.yaml") -> str:
    data = yaml.safe_load(blueprint.read_text(encoding="utf-8"))
    return data["services"][0]["buildCommand"]


def main() -> int:
    command = build_command()
    print(f"render build: {command}", flush=True)
    code = subprocess.run(command, shell=True, cwd=ROOT).returncode
    if code:
        print(f"render build: FAILED (exit {code})", file=sys.stderr)
    else:
        print("render build: ok")
    return code


if __name__ == "__main__":
    sys.exit(main())
