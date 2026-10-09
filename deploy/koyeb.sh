#!/usr/bin/env bash
# Koyeb, second to Render. Needs the koyeb CLI, logged in (`koyeb login`).
# Nothing here runs by itself: read it, set REPO, then run one recipe.
#
#   bash deploy/koyeb.sh secrets   # store the token, allowlist and keys once
#   bash deploy/koyeb.sh free      # Recipe B: free web service + a pinger on /healthz
#   bash deploy/koyeb.sh worker    # Recipe C: paid worker, eco-nano $1.61/mo, no pinger
#
# Koyeb's free instance (512 MB, 0.1 vCPU, one per organization, Frankfurt or
# Washington) is web-service only and cannot be a worker. Like Render's, a web
# service needs inbound traffic to stay up, so point a pinger at
# https://<app>-<org>.koyeb.app/healthz every 10 minutes. Prices from
# koyeb.com/docs/reference/instances, read 2026-10-09: eco-nano $1.61/mo,
# nano $2.68/mo.
#
# The provider order falls through Gemini -> Groq -> Hugging Face: Gemini's free
# tier allows 20 requests per day per project per model, and once it answers 429
# the factory retries, then moves to the next keyed provider. Providers with no
# key are skipped, so the GROQ_API_KEY and HF_TOKEN secrets are optional.
# BOT_ACCESS_PASSWORD is optional too: the shared /auth password, off when empty.
set -euo pipefail

REPO="${REPO:-github.com/AdamJannoud/Father-Agent}"
BRANCH="${BRANCH:-main}"
APP="${APP:-father-agent-bot}"
REGION="${REGION:-fra}"

common=(
  --git "$REPO" --git-branch "$BRANCH"
  --git-builder docker --git-docker-dockerfile deploy/Dockerfile
  --regions "$REGION"
  --env "TELEGRAM_BOT_TOKEN={{secret.TELEGRAM_BOT_TOKEN}}"
  --env "TELEGRAM_ALLOWED_USERS={{secret.TELEGRAM_ALLOWED_USERS}}"
  --env "GEMINI_API_KEY={{secret.GEMINI_API_KEY}}"
  --env "GROQ_API_KEY={{secret.GROQ_API_KEY}}"
  --env "HF_TOKEN={{secret.HF_TOKEN}}"
  --env "BOT_ACCESS_PASSWORD={{secret.BOT_ACCESS_PASSWORD}}"
  --env "FATHER_PROVIDER_ORDER=gemini,groq,huggingface"
  --env "PORT=8000"
)

case "${1:-}" in
  secrets)
    for name in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS GEMINI_API_KEY GROQ_API_KEY HF_TOKEN \
                BOT_ACCESS_PASSWORD; do
      read -r -s -p "$name (empty to skip): " value; echo
      [ -n "$value" ] && koyeb secrets create "$name" --value "$value"
    done
    ;;
  free)
    koyeb app init "$APP" "${common[@]}" --type web --instance-type free \
      --ports 8000:http --routes /:8000 --checks 8000:http:/healthz
    echo "now add a pinger: GET https://<your-app-url>/healthz every 10 minutes"
    ;;
  worker)
    koyeb app init "$APP" "${common[@]}" --type worker --instance-type eco-nano
    ;;
  *)
    sed -n '2,19p' "$0"
    exit 2
    ;;
esac
