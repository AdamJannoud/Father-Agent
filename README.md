# The Father Agent

[![ci](https://github.com/AdamJannoud/Father-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/AdamJannoud/Father-Agent/actions/workflows/ci.yml)

**An open-source agent factory.** You type one line of English; it plans, writes
and validates a professional async Python sub-agent into `subagents/<slug>/`,
and delivers it as whatever the sentence asked for: a command-line agent, a
Streamlit or FastAPI dashboard, a JSON API, or an aiogram Telegram bot, each
with its Dockerfile, its free-host config, a pinned `requirements.txt` and a
one-command deploy. You own every line, it runs on a laptop, and inference
costs nothing.

```text
$ python main.py new "a Solana wallet watcher that logs balance changes every 60s and plots them"
father spec     1/6  planning · provider groq/qwen/qwen3.8-27b
father spec     2/6  wallet_watcher · blockchain · httpx + pandas + matplotlib
father code     3/6  wrote agent.py: 5 classes, 10 methods, docstrings · 288 lines
father code     3/6  wrote test_agent.py · 83 lines
father validate 4/6  ast ok · compile ok · ruff ok · imports ok · consistency ok
father validate 4/6  policy ok · bundle ok · deploy ok · secrets ok · nothing was executed
father delivery 5/6  Dockerfile · .dockerignore · .env.example · bootstrap.py · requirements.txt
father write    6/6  subagents/wallet_watcher/ · 9 files
next: python -m subagents.wallet_watcher.agent --once
```

Ask for a bot and you get a bot:

```text
$ python main.py new "a Telegram bot that watches a Solana wallet and DMs me on changes"
father spec     2/6  solana_bot · blockchain · interface telegram · aiogram · httpx
father code     3/6  wrote agent.py: 4 classes, 9 methods, docstrings · 253 lines
father code     3/6  wrote bot.py: telegram · aiogram · 296 lines
father code     3/6  wrote test_agent.py · 111 lines
father validate 4/6  policy ok · bundle ok · deploy ok · secrets ok · nothing was executed
father delivery 5/6  Dockerfile · .dockerignore · render.yaml · .env.example · bootstrap.py · requirements.txt · scripts/setup_bot.py
father write    6/6  subagents/solana_bot/ · 15 files
next: cd subagents/solana_bot && cp .env.example .env && python bot.py
```

It covers any Python specialisation: data/ML, web and APIs, scraping, automation,
blockchain, plotting and so on. The planner picks the real libraries the job
needs (pandas, scikit-learn, httpx, beautifulsoup4, web3, matplotlib...).

## What it writes

```text
common to every interface
  agent.py          the async core: WalletWatcher · SolanaBalanceClient · BalanceStore · ...
  test_agent.py     offline tests with a fake data source: no network, no key
  README.md         run it · env vars · how to deploy
  spec.json         the plan it was built from; rebuild with --from-spec
  requirements.txt  pinned, complete for the bundle
  .env.example      every variable it reads, all empty
  bootstrap.py      opt-in install of missing declared packages
  Dockerfile        python:3.12-slim, non-root, CMD runs the entry file
  .dockerignore

added by the interface
  cli        nothing extra
  web        app.py               Streamlit, or FastAPI with an HTML dashboard
             deploy/huggingface/  a complete Space (Docker SDK, port 7860)
             deploy/render/       render.yaml blueprint (free web service)
  telegram   bot.py               aiogram 3: polling locally, webhook on a host
             scripts/setup_bot.py getMe · command menu · optional setWebhook
             deploy/render/       webhook mode
  api        app.py               FastAPI: /health · /run · /history · /latest
             deploy/huggingface/  deploy/render/
```

Every generated sub-agent follows the same standards: asyncio for I/O, classes
and methods with docstrings, `logging` (never `print`), try/except around every
external call, configuration from environment variables, and an argparse
`main()` with `--once`.

## How a command becomes files

```text
your command ─▶ 1 Planner ─▶ 2 Coder ─────▶ 3 Validator ─▶ 4 Writer
                strict JSON   agent.py,      nine checks     subagents/<slug>/
                spec +        app.py/bot.py, never runs      (only stage that
                delivery      test_agent.py  code            touches disk)
                block              ▲
                      ▲            │   2b Templates (exact text, no model):
                      │            │      Dockerfile · requirements.txt · .env.example
                      │            │      bootstrap.py · deploy/huggingface · deploy/render
                      └── provider layer (free only): Groq free tier → Hugging Face → (mock)
                          every step logged to logs/father-agent.log
```

1. **Planner** sends a versioned prompt (`prompts/planner.md`) with the JSON schema
   of the spec. The reply is parsed and checked against that schema
   (`father_agent/spec.py`) before any code is written. A malformed spec is sent
   back once with the errors. The spec carries a `delivery` block
   (`interface`, `framework`, `deploy[]`); see [Interfaces](#interfaces-and-deployment).
2. **Coder** writes `agent.py`, then the interface file (`app.py` or `bot.py`,
   from `prompts/coder_streamlit.md`, `coder_fastapi.md` or `coder_telegram.md`),
   then `test_agent.py` against both. Each reply goes straight into the
   validator; a rejected file goes back to the model with the exact problems, up
   to `FATHER_MAX_REPAIR_ATTEMPTS` times. The plumbing (Dockerfile, host
   manifests, requirements, `.env.example`, `bootstrap.py`) is exact static text
   from `father_agent/delivery.py`, never model output.
3. **Validator gate** (`father_agent/validator.py`) checks every file while it is
   still a string, and **never imports or executes generated code**:
   - `ast` — it parses;
   - `compile` — `py_compile` in a throw-away temp dir;
   - `ruff` — `E9,F,B` (syntax, undefined names, unused imports, bugbear);
   - `imports` — every import is standard library, a declared dependency, or a
     sibling file, resolved statically from the AST;
   - `consistency` — the spec's classes, methods and `main()` exist; the tests
     import only names `agent.py` defines;
   - `policy` — docstrings, `async def`, `logging`, try/except; no `print`,
     `eval`/`exec`, `os.system`, `shell=True`, or paid-API SDKs;
   - `bundle` — `requirements.txt` covers every third-party import in the bundle,
     and `.env.example` lists every declared variable and every one the code reads;
   - `deploy` — the Dockerfile's `CMD` runs a file that exists; each host manifest
     (the Space README frontmatter, `render.yaml`) parses and points at files that
     exist; every copy under `deploy/` matches the root file;
   - `secrets` — no literal token, API key, private key or long hex string
     anywhere in the bundle, and `.env.example` holds no values.
4. **Writer** stages the bundle in a sibling folder and renames it into place,
   so you get the whole sub-agent or nothing. An existing folder is never
   overwritten without `--force`.

If any file fails the gate, nothing is written and the problems are printed:

```text
father: agent.py failed the validator gate after 3 attempt(s); nothing was written. Problems:
  - ast: line 1: invalid syntax
```

## Install (Python 3.11+)

```bash
git clone <your fork of this repo> father-agent && cd father-agent
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py doctor
```

Or run everything (venv, install, lint, tests, an end-to-end generation) in one go:

```bash
bash scripts/verify.sh
```

`verify.sh` needs no key, and after the first install it needs no network: the
suite runs against the offline mock provider and blocks real sockets, and the
end-to-end step builds one sub-agent per interface (cli, telegram, web, api).

## Add your free keys

Inference is **free only**. The config accepts exactly these hosts and refuses
anything else at start-up, so a paid API cannot be reached by accident:

| Provider | Free key | Default model |
| --- | --- | --- |
| Groq free tier | <https://console.groq.com/keys> → `GROQ_API_KEY` | `qwen/qwen3.8-27b` |
| Hugging Face Serverless Inference | <https://huggingface.co/settings/tokens> → `HF_TOKEN` | `Qwen/Qwen2.5-72B-Instruct` |

```bash
cp .env.example .env
# edit .env and paste your key(s) after GROQ_API_KEY= and/or HF_TOKEN=
python main.py doctor --online      # checks each key with GET /models (no tokens spent)
```

`.env` is git-ignored; `.env.example` only has empty placeholders. Keys are
also scrubbed from every log line.

**No key yet?** Everything still works: with no key the factory uses an offline,
deterministic **mock provider** that plans with keyword rules and writes from
built-in templates (blockchain, scraping, data/ML, web API and automation
sub-agents), and renders every interface: Streamlit and FastAPI apps and an
aiogram bot. It is how the test suite proves the factory end to end without a
network. Real models write far more tailored code, so add a key when you can.

### When a free tier says stop

A `429`, a `5xx` or a timeout is retried with backoff (honouring `Retry-After`);
when retries run out, or a key is rejected or a model is retired, the chain
switches to the next provider instead of ending the run:

```text
father spec     1/6  groq/qwen/qwen3.8-27b → 429, waiting 3s (attempt 2 of 3)
father spec     1/6  groq/qwen/qwen3.8-27b → 429: ...; switching to huggingface/Qwen/Qwen2.5-72B-Instruct
```

Every provider call runs under an asyncio timeout (`FATHER_REQUEST_TIMEOUT`) and
a concurrency semaphore, so a hung free tier cannot freeze the factory. Set
`FATHER_ALLOW_MOCK_FALLBACK=true` to fall back to the offline mock as a last resort.

### Hardware

The laptop only orchestrates: the package is pure Python and needs no GPU. A
small local model is **off by default** (a 2 GB card like a Quadro P620 cannot
hold one that writes good code), but any llama.cpp / OpenAI-compatible server on
`localhost` can be added with `FATHER_LOCAL_LLM_URL` (see `.env.example`).

## Commands

| Command | What it does |
| --- | --- |
| `python main.py new "<command>"` | plan, write, validate and save a sub-agent (`generate` is an alias) |
| `  --out DIR` | parent folder instead of `subagents/` |
| `  --provider auto\|groq\|huggingface\|local\|mock` | force one provider |
| `  --interface cli\|web\|telegram\|api` | override the interface read from the command |
| `  --framework streamlit\|fastapi\|aiogram` | override the framework (alone, it picks its interface) |
| `  --dry-run` | plan only and print the spec |
| `  --from-spec subagents/x/spec.json` | rebuild the code from an existing spec |
| `  --force` | replace an existing sub-agent folder |
| `python main.py list` | list generated sub-agents |
| `python main.py show <slug>` | show a sub-agent's spec and re-run the gate on its files |
| `python main.py deploy <slug> --target docker\|hf-spaces\|render` | refresh that target's folder, re-run the gate, print the exact commands (`docker build` runs when Docker is installed; `--no-build` skips it) |
| `python main.py capabilities` | list the capability packs installed, proposable from the local catalogue, and rejected by the licence allowlist |
| `python main.py evolve "<task>"` | detect a missing capability and propose an additive upgrade, without generating anything; asks `[y/N]` |
| `  --yes` | approve without a prompt (scripted use); still declined when `CI=true` |
| `  --log` | print the evolution ledger |
| `python main.py providers [--check]` | show the provider chain and key status |
| `python main.py doctor [--online]` | check Python, config, providers, prompts, ruff, paths |

Exit codes: `0` success, `1` the factory failed (for example the gate rejected a
file), `2` bad usage or configuration.

## Running a sub-agent

The factory never runs what it writes. Read the code first, then:

```bash
pip install -r subagents/wallet_watcher/requirements.txt
export WALLET_ADDRESS=<a Solana address>
python -m subagents.wallet_watcher.agent --once     # one cycle
cd subagents/wallet_watcher && python -m pytest -q   # its own offline tests
```

`python bootstrap.py` inside a sub-agent reports any declared package that is
missing, and installs it only when you pass `--auto-install` (or set
`FATHER_AUTO_INSTALL=1`). It installs exactly what `requirements.txt` lists,
never a name the code merely imports.

## Interfaces and deployment

The interface is read from your sentence, and `--interface` / `--framework`
override it:

| The command says | Interface | You get |
| --- | --- | --- |
| "a Telegram bot that ..." | `telegram` · aiogram | `bot.py`: polling on your laptop, `--webhook` on a host, `--dry-run` proves the handlers with no token; `scripts/setup_bot.py` checks the token with `getMe`, registers the command menu and can call `setWebhook` |
| "a dashboard that ...", "a web app ..." | `web` · streamlit (fastapi when you say FastAPI) | `app.py` over the same agent core |
| "a service that exposes ... over HTTP", "a FastAPI microservice" | `api` · fastapi | `app.py` with `/health`, `/run`, `/history`, `/latest` |
| anything else | `cli` | today's agent, unchanged |

Consuming an API ("track the bitcoin price API") is not serving one, so it stays
`cli`. A spec with no `delivery` block (every `spec.json` written before this
existed) is a `cli` sub-agent and rebuilds as one.

Every bundle has a `Dockerfile`. Web and api bundles also get
`deploy/huggingface/`, a complete Hugging Face Space using the Docker SDK
(Hugging Face's config reference lists only `gradio`, `docker` and `static` as
Space SDKs, so Streamlit runs in Docker on port 7860), and `deploy/render/`, a
Render Blueprint for one free web service. Telegram bundles get `deploy/render/`
in webhook mode. Each folder is self-contained, so you can `git push` it as its
own repository:

```bash
python main.py deploy solana_dashboard --target hf-spaces
# prepared subagents/solana_dashboard/deploy/huggingface/ (Dockerfile, README.md, agent.py, app.py, requirements.txt)
# gate: ... bundle ok · deploy ok · secrets ok · nothing was executed
# next (hf-spaces):
#   # 1. create an empty Space (SDK: Docker) at https://huggingface.co/new-space
#   cd subagents/solana_dashboard/deploy/huggingface
#   ...
```

The factory prepares a deploy; it does not push one and never holds a token.
Secrets live in `.env` (git-ignored, docker-ignored) or in the host's own
secret settings.

## Use it from smolagents

`father_agent.integrations.smolagents_tool.make_factory_tool()` returns a
[smolagents](https://github.com/huggingface/smolagents) `Tool`, so a smolagents
agent can ask the Father Agent for a new sub-agent:

```python
from smolagents import CodeAgent, InferenceClientModel
from father_agent.integrations.smolagents_tool import make_factory_tool

agent = CodeAgent(tools=[make_factory_tool()], model=InferenceClientModel())
agent.run("Build me a sub-agent that scrapes Hacker News headlines hourly.")
```

## Self-evolution

The factory checks every task against a registry of capability packs
(`father_agent/evolution/registry.json`) **before** it generates anything. When
a task needs something no pack provides, it proposes an additive upgrade from a
curated, local catalogue and waits for you:

```text
$ python main.py evolve "watch a kafka topic and alert on spikes"
capability check … gap found
  need : message-queue client (kafka)  (task mentions "kafka")
  have : http · schedule · charts · telegram · database · api · web · scraping · ml
proposed upgrade — nothing written yet
  add    capability pack "kafka"
         templates/consumer.py + pack.json
  why    the task needs a Kafka consumer; no installed pack covers message queues
         (task mentions "kafka")
  deps   kafka-python >=2.0,<3 · Apache-2.0 · open-source, no account, no key
  writes father_agent/evolution/packs/kafka/** · registry entry · requirements-packs.txt · ledger
  never  core modules or requirements.txt — manifest 68 files, sha256 pinned
  gate   ast ok · compile ok · ruff ok · licence ok (checked in memory)
  diff   sha256:cf1584e53711084b63da7fc36d20bb4a98d9c2b1c0e9e56edb345a50cb41d5c3
apply this upgrade? [y/N] n
  declined — nothing written (answered no).
core manifest intact (68 files, hashes unchanged)
ledger ← {"decision": "declined", "pack": "kafka", "ts": "…", "task": "watch a kafka topic and alert on spikes"}
```

`python main.py new` runs the same check after it generates, and asks the same
question when it finds a gap. The guard rails are code, not promises:

- **Never touches the core.** `father_agent/evolution/core_manifest.json` pins
  every core file by sha256. The applier refuses any path in it (and anything
  outside `packs/<name>/`, the registry and `requirements-packs.txt`), refuses
  to run if the core already drifted, and re-checks every hash after applying.
  `tests/test_evolution.py` proves a core-file write is refused.
- **Never applies without you.** The default is No. With no terminal (a pipe,
  cron, CI) it declines and says how to re-run it; `--yes` exists for scripts
  but is still declined when `CI=true`. There is no "remember my answer".
- **Only free, open-source components.** Each catalogue dependency carries a
  licence, and the validator's `licence` check rejects anything not on the
  allowlist (MIT, BSD-2/3-Clause, Apache-2.0, ISC, PSF-2.0, MPL-2.0, LGPL), any
  paid API SDK, and anything that needs an account or a token.
- **Offline.** Planning reads local JSON and the offline planner; it makes no
  network call and uses no hosted or paid model.
- **On the record.** Every proposal and decision is a line in
  `father_agent/evolution/ledger.jsonl` (task, pack, dependencies, decision,
  diff hash); `python main.py evolve --log` prints it.

Pack dependencies go to `requirements-packs.txt`, never `requirements.txt`.
Changed a core file on purpose? Re-pin it with
`python -m father_agent.evolution --write` (the test suite fails until you do).

## Project layout

```text
main.py                     CLI entry point
father_agent/
  config.py                 Config: .env + environment, free-only endpoint allow-list
  logging_setup.py          rotating log file, progress lines, key redaction
  spec.py                   SubAgentSpec: the planner → coder JSON contract
  prompts.py                PromptLibrary: versioned templates from prompts/
  providers/                Provider base, OpenAI-compatible HTTP client, mock, chain
  planner.py                Planner: command → spec
  coder.py                  Coder: spec → files, through the gate, with repair
  validator.py              Validator: ast · compile · ruff · imports · consistency · policy
                            · bundle · deploy · secrets
  delivery.py               interface inference + the static kit: requirements, .env.example,
                            bootstrap.py, Dockerfile, deploy/huggingface, deploy/render
  factory.py                Factory: the six stages + atomic writer
  heuristics.py, templates.py   the offline mock's planning rules and code templates
  interface_templates.py    the offline mock's app.py (Streamlit, FastAPI) and bot.py (aiogram)
  integrations/             smolagents Tool
  evolution/                self-evolution: registry.json, catalogue.json, templates/,
                            detector, consent gate, applier, core_manifest.json, ledger
prompts/                    planner.md, coder_agent.md, coder_tests.md,
                            coder_streamlit.md, coder_fastapi.md, coder_telegram.md
tests/                      offline test suite (sockets blocked)
scripts/verify.sh           install + lint + test + end-to-end check (CI runs it as is)
.github/workflows/ci.yml    CI: ruff, and verify.sh on Python 3.11 and 3.12; no secrets
```

## Extending it

- **Better prompts:** edit `prompts/*.md` and bump the `prompt-version` header;
  the version is logged with every run and shown by `doctor`.
- **Stricter or looser gate:** `Validator(ruff_rules=..., require_ruff=True)`, or
  add a check method in `validator.py`.
- **Another free provider:** anything OpenAI-compatible is one
  `OpenAICompatProvider(...)`; add its host to `FREE_HOSTS` in `config.py` only
  if it is genuinely free.
- **More offline templates:** add a `Profile` in `heuristics.py` and a data
  source template in `templates.py`; `tests/test_validator.py` checks every
  template passes the gate.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
bash scripts/verify.sh      # what CI runs, key-free and offline after the install
```

CI (`.github/workflows/ci.yml`) runs on every push and pull request: a `lint`
job (ruff alone) and a `tests` job that runs `scripts/verify.sh` on Python 3.11
and 3.12. It needs no secrets, so it runs the same on a fork.

## Licence

MIT. See [LICENSE](LICENSE).
