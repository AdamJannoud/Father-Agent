#!/usr/bin/env bash
# Install the Father Agent into a local virtualenv and prove it works, from a
# clean checkout. Needs NO API key and NO network beyond the first install:
# every check runs against the offline mock provider.
#
#   bash scripts/verify.sh
#
# Steps: venv + deps → ruff on the project → pytest → the bot extra (bot --check
# when aiogram is installed, the install hint when it is not) → main.py --help →
# main.py doctor → end-to-end generations into sample_output/: the cli sample,
# then one per delivery interface (telegram, web/streamlit, web/fastapi, api),
# each re-validated from disk, plus a deploy dry run, then the self-evolution
# smoke check: capabilities lists packs, and evolve, run against a scratch
# checkout of the shipped state, on a task needing a pack we do not ship
# proposes it, declines with no terminal, and writes no pack.
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
   && "$VPY" -c 'import httpx, pydantic, dotenv, yaml, pytest' 2>/dev/null; then
  say "dependencies already installed (requirements unchanged)"
else
  say "installing requirements-dev.txt"
  if PIP_DEFAULT_TIMEOUT=20 "$VPY" -m pip install -q --disable-pip-version-check \
       -r requirements-dev.txt; then
    echo "$WANT" > "$STAMP"
  else
    "$VPY" -c 'import httpx, pydantic, dotenv, yaml, pytest' 2>/dev/null \
      || fail "pip install failed and the dependencies are not installed (offline?)"
    say "pip install failed (offline?) but the core dependencies are present; continuing"
  fi
fi

# The optional Telegram bot extra, on top of the core: FATHER_REQUIRE_BOT=1 (CI)
# installs it and makes tests/test_bot.py fail rather than skip without it. A
# second step, because aiogram 3.31 caps pydantic below the core's 2.14.0 pin.
if [ "${FATHER_REQUIRE_BOT:-}" = "1" ]; then
  BOT_STAMP="$VENV/.father-bot-requirements.sha256"
  BOT_WANT="$(cat requirements-bot.txt | sha256sum | cut -d' ' -f1)"
  if [ "$(cat "$BOT_STAMP" 2>/dev/null || true)" = "$BOT_WANT" ] \
     && "$VPY" -c 'import aiogram, aiohttp' 2>/dev/null; then
    say "bot extra already installed (requirements-bot.txt unchanged)"
  else
    say "installing requirements-bot.txt (FATHER_REQUIRE_BOT=1)"
    PIP_DEFAULT_TIMEOUT=20 "$VPY" -m pip install -q --disable-pip-version-check \
      -r requirements-bot.txt || fail "could not install the bot extra"
    echo "$BOT_WANT" > "$BOT_STAMP"
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

# The Telegram bot is an optional extra: with it, its offline suite ran above
# and `bot --check` must pass; without it, `bot` must explain the install.
if "$VPY" -c 'import aiogram' 2>/dev/null; then
  say "python main.py bot --check (bot extra installed; placeholder token, nothing contacted)"
  TELEGRAM_BOT_TOKEN="123456789:verify-placeholder-not-a-token" \
    "$VPY" main.py bot --check >/dev/null || fail "bot --check failed"
  echo "ok: the offline bot suite (tests/test_bot.py) ran in the pytest step above"
else
  say "python main.py bot (bot extra NOT installed: must explain the install)"
  BOT_ERR="$("$VPY" main.py bot 2>&1 >/dev/null || true)"
  printf '%s\n' "$BOT_ERR" | grep -q 'requirements-bot.txt' \
    || fail "bot without aiogram did not point at requirements-bot.txt"
  echo "ok: core install unharmed; tests/test_bot.py skipped (pip install -r requirements-bot.txt)"
fi

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

# One generation per delivery interface, still offline and key-free: the mock
# provider renders app.py / bot.py and the factory writes the deploy kit.
# Format: slug|files that must exist|extra flags|command
INTERFACES=(
  "solana_bot|bot.py scripts/setup_bot.py deploy/render/render.yaml||a Telegram bot that watches a Solana wallet and DMs me on changes"
  "solana_dashboard|app.py deploy/huggingface/README.md deploy/render/render.yaml||a dashboard that tracks Solana priority fees and plots the last hour"
  "bitcoin_price_tracker|app.py deploy/huggingface/Dockerfile|--interface web --framework fastapi|track the bitcoin price API every 5 minutes"
  "bitcoin_service|app.py deploy/render/app.py||a service that exposes the bitcoin price over an HTTP API"
)
for row in "${INTERFACES[@]}"; do
  IFS='|' read -r slug files flags command <<< "$row"
  say "end-to-end (delivery): $slug ← \"$command\" $flags"
  # shellcheck disable=SC2086
  "$VPY" main.py new --provider mock --force --quiet --out "$SAMPLE_DIR" $flags "$command" \
    || fail "generation of $slug failed"
  for f in agent.py test_agent.py README.md spec.json requirements.txt .env.example \
           bootstrap.py Dockerfile .dockerignore $files; do
    [ -s "$SAMPLE_DIR/$slug/$f" ] || fail "missing $SAMPLE_DIR/$slug/$f"
  done
  "$VPY" main.py show "$slug" --out "$SAMPLE_DIR" >/dev/null \
    || fail "re-validating $slug from disk failed"
  echo "ok: $SAMPLE_DIR/$slug/ written and re-validated (bundle · deploy · secrets)"
done

say "python main.py deploy (prepare only: prints commands, pushes nothing)"
"$VPY" main.py deploy solana_bot --out "$SAMPLE_DIR" --target render >/dev/null \
  || fail "deploy --target render failed"
"$VPY" main.py deploy solana_dashboard --out "$SAMPLE_DIR" --target docker --no-build \
  >/dev/null || fail "deploy --target docker failed"
echo "ok"

say "python main.py capabilities"
"$VPY" main.py capabilities | grep -q "installed capability packs" \
  || fail "capabilities did not list the installed packs"
echo "ok"

# Against a scratch checkout of the shipped state, not this tree: the README
# documents applying an upgrade in your own checkout, and an installed pack is
# no longer a gap, so evolve would rightly propose nothing here.
say "python main.py evolve (no terminal: must propose, decline, write no pack)"
EVOLVE_ROOT="$(mktemp -d)"
trap 'rm -rf "$EVOLVE_ROOT"' EXIT
"$VPY" -c 'import sys, pathlib; from father_agent.evolution.pristine import write_pristine_checkout as write; write(pathlib.Path(sys.argv[1]))' "$EVOLVE_ROOT" \
  || fail "could not build a pristine evolution checkout"
EVOLVE_OUT="$(FATHER_EVOLUTION_ROOT="$EVOLVE_ROOT" "$VPY" main.py evolve "watch a kafka topic and alert on spikes" </dev/null)" \
  || fail "evolve exited non-zero"
printf '%s\n' "$EVOLVE_OUT" | grep -q 'proposed upgrade' || fail "evolve proposed nothing"
printf '%s\n' "$EVOLVE_OUT" | grep -q '^declined' || fail "evolve did not decline without a terminal"
printf '%s\n' "$EVOLVE_OUT" | grep -q 'core manifest intact' || fail "core manifest not intact"
[ ! -e "$EVOLVE_ROOT/father_agent/evolution/packs/kafka" ] || fail "evolve wrote a pack without consent"
"$VPY" -m father_agent.evolution >/dev/null || fail "the core does not match core_manifest.json"
echo "ok: proposed, declined, nothing written, core manifest intact"

say "all checks passed"
