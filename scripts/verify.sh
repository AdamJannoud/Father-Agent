#!/usr/bin/env bash
# Install the Father Agent into a local virtualenv and prove it works, from a
# clean checkout. Needs NO API key and NO network beyond the first install:
# every check runs against the offline mock provider.
#
#   bash scripts/verify.sh
#
# Steps: venv + deps → ruff on the project → pytest → main.py --help →
# main.py doctor → one end-to-end generation into sample_output/.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"
VENV="${VENV:-$ROOT/.venv}"
SAMPLE_DIR="$ROOT/sample_output"
SAMPLE_COMMAND="a Solana wallet watcher that logs balance changes every 60s and plots them"

say()  { printf '\nverify: %s\n' "$*"; }
fail() { printf '\nverify: FAILED: %s\n' "$*" >&2; exit 1; }

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "Python 3.11+ is required ($("$PY" --version 2>&1) found). Set PYTHON=python3.11"

if [ ! -x "$VENV/bin/python" ]; then
  say "creating virtualenv in $VENV"
  "$PY" -m venv "$VENV" || fail "could not create a venv (on Debian/Ubuntu: apt install python3-venv)"
fi
VPY="$VENV/bin/python"

# Install only when the requirement files changed since the last successful
# install, so a re-run works offline.
STAMP="$VENV/.father-requirements.sha256"
WANT="$(cat requirements.txt requirements-dev.txt | sha256sum | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ] \
   && "$VPY" -c 'import httpx, pydantic, dotenv, pytest' 2>/dev/null; then
  say "dependencies already installed (requirements unchanged)"
else
  say "installing requirements-dev.txt"
  if PIP_DEFAULT_TIMEOUT=20 "$VPY" -m pip install -q --disable-pip-version-check \
       -r requirements-dev.txt; then
    echo "$WANT" > "$STAMP"
  else
    "$VPY" -c 'import httpx, pydantic, dotenv, pytest' 2>/dev/null \
      || fail "pip install failed and the dependencies are not installed (offline?)"
    say "pip install failed (offline?) but the core dependencies are present; continuing"
  fi
fi

# No keys and no .env: everything below must work offline.
export FATHER_ENV_FILE="$ROOT/.verify-no-env"
unset GROQ_API_KEY HF_TOKEN FATHER_LOCAL_LLM_URL || true

if "$VPY" -m ruff --version >/dev/null 2>&1; then
  say "ruff check (project)"
  "$VPY" -m ruff check . || fail "ruff found problems"
fi

say "pytest"
"$VPY" -m pytest -q || fail "test suite failed"

say "python main.py --help"
"$VPY" main.py --help >/dev/null || fail "main.py --help"
echo "ok"

say "python main.py doctor"
"$VPY" main.py doctor || fail "doctor reported a problem"

say "end-to-end: python main.py new (mock provider) → $SAMPLE_DIR"
"$VPY" main.py new --provider mock --force --out "$SAMPLE_DIR" "$SAMPLE_COMMAND" \
  || fail "end-to-end generation failed"
for f in agent.py test_agent.py README.md spec.json; do
  [ -s "$SAMPLE_DIR/wallet_watcher/$f" ] || fail "missing $SAMPLE_DIR/wallet_watcher/$f"
done
"$VPY" main.py show wallet_watcher --out "$SAMPLE_DIR" >/dev/null \
  || fail "re-validating the written files failed"
echo "ok: $SAMPLE_DIR/wallet_watcher/ written and re-validated"

say "all checks passed"
