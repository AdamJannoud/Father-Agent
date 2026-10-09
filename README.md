# The Father Agent

**An open-source agent factory.** You type one line of English; it plans, writes
and validates a professional async Python sub-agent into `subagents/<slug>/`.
You own every line, it runs on a laptop, and inference costs nothing.

```text
$ python main.py new "a Solana wallet watcher that logs balance changes every 60s and plots them"
father spec     1/5  planning · provider groq/qwen/qwen3.8-27b
father spec     2/5  wallet_watcher · blockchain · httpx + pandas + matplotlib
father code     3/5  wrote agent.py: 5 classes, 10 methods, docstrings · 288 lines
father code     3/5  wrote test_agent.py · 83 lines
father validate 4/5  ast ok · compile ok · ruff ok · imports ok · consistency ok · policy ok · nothing was executed
father write    5/5  subagents/wallet_watcher/ · agent.py · test_agent.py · README.md · spec.json
next: python -m subagents.wallet_watcher.agent --once
```

It covers any Python specialisation: data/ML, web and APIs, scraping, automation,
blockchain, plotting and so on. The planner picks the real libraries the job
needs (pandas, scikit-learn, httpx, beautifulsoup4, web3, matplotlib...).

## What it writes

```text
subagents/wallet_watcher/
  agent.py        WalletWatcher · SolanaBalanceClient · BalanceStore · BalancePlotter · Settings
  test_agent.py   4 tests with a fake data source: no network, no key
  README.md       what it does, how to run it, which env vars/keys it needs
  spec.json       the plan it was built from; rebuild with --from-spec
```

Every generated sub-agent follows the same standards: asyncio for I/O, classes
and methods with docstrings, `logging` (never `print`), try/except around every
external call, configuration from environment variables, and an argparse
`main()` with `--once`.

## How a command becomes files

```text
your command ─▶ 1 Planner ─▶ 2 Coder ─▶ 3 Validator ─▶ 4 Writer
                strict JSON   one file   ast · compile    subagents/<slug>/
                spec          at a time  ruff · imports   (only stage that
                                         never runs code   touches disk)
                      ▲            ▲
                      └── provider layer (free only): Groq free tier → Hugging Face → (mock)
                          every step logged to logs/father-agent.log
```

1. **Planner** sends a versioned prompt (`prompts/planner.md`) with the JSON schema
   of the spec. The reply is parsed and checked against that schema
   (`father_agent/spec.py`) before any code is written. A malformed spec is sent
   back once with the errors.
2. **Coder** writes `agent.py`, then `test_agent.py` against it. Each reply goes
   straight into the validator; a rejected file goes back to the model with the
   exact problems, up to `FATHER_MAX_REPAIR_ATTEMPTS` times.
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
     `eval`/`exec`, `os.system`, `shell=True`, or paid-API SDKs.
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
suite runs against the offline mock provider and blocks real sockets.

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
sub-agents). It is how the test suite proves the factory end to end without a
network. Real models write far more tailored code, so add a key when you can.

### When a free tier says stop

A `429`, a `5xx` or a timeout is retried with backoff (honouring `Retry-After`);
when retries run out, or a key is rejected or a model is retired, the chain
switches to the next provider instead of ending the run:

```text
father spec     1/5  groq/qwen/qwen3.8-27b → 429, waiting 3s (attempt 2 of 3)
father spec     1/5  groq/qwen/qwen3.8-27b → 429: ...; switching to huggingface/Qwen/Qwen2.5-72B-Instruct
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
| `  --dry-run` | plan only and print the spec |
| `  --from-spec subagents/x/spec.json` | rebuild the code from an existing spec |
| `  --force` | replace an existing sub-agent folder |
| `python main.py list` | list generated sub-agents |
| `python main.py show <slug>` | show a sub-agent's spec and re-run the gate on its files |
| `python main.py providers [--check]` | show the provider chain and key status |
| `python main.py doctor [--online]` | check Python, config, providers, prompts, ruff, paths |

Exit codes: `0` success, `1` the factory failed (for example the gate rejected a
file), `2` bad usage or configuration.

## Running a sub-agent

The factory never runs what it writes. Read the code first, then:

```bash
pip install httpx pandas matplotlib                  # the libraries in its README
export WALLET_ADDRESS=<a Solana address>
python -m subagents.wallet_watcher.agent --once     # one cycle
cd subagents/wallet_watcher && python -m pytest -q   # its own offline tests
```

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
  factory.py                Factory: the five stages + atomic writer
  heuristics.py, templates.py   the offline mock's planning rules and code templates
  integrations/             smolagents Tool
prompts/                    planner.md, coder_agent.md, coder_tests.md
tests/                      offline test suite (sockets blocked)
scripts/verify.sh           install + lint + test + end-to-end check
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
```

## Licence

MIT. See [LICENSE](LICENSE).
