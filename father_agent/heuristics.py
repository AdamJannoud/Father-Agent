"""Deterministic, offline planning used by the mock provider.

When no free key is configured (and in the test suite) the factory still has
to turn a sentence into a sensible spec. This module does that with keyword
rules: it picks a specialisation (blockchain, scraping, data/ML, web API,
automation), the real libraries that specialisation needs, a slug, a polling
interval and the class layout. A real model does this far better; these
rules exist so the whole pipeline is provable with no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .delivery import infer_delivery


@dataclass(frozen=True)
class Profile:
    """One specialisation the offline planner knows how to scaffold."""

    domain: str
    keywords: tuple[str, ...]
    fetch: str
    dependencies: tuple[tuple[str, str], ...]
    client: str
    store: str
    plotter: str
    target_env: str
    default_target: str
    interval: int
    value_label: str


PROFILES: tuple[Profile, ...] = (
    Profile("blockchain", ("solana", "wallet", "ethereum", "web3", "token", "crypto",
                           "blockchain", "nft", "defi", "lamports", "balance"),
            "rpc", (("httpx", "async JSON-RPC calls to a public node"),),
            "BalanceClient", "BalanceStore", "BalancePlotter", "WALLET_ADDRESS", "", 60,
            "balance"),
    Profile("scraping", ("scrape", "scraper", "scraping", "crawl", "crawler", "website",
                         "web page", "webpage", "html", "headlines", "listings"),
            "html", (("httpx", "async page downloads"),
                     ("beautifulsoup4", "HTML parsing with CSS selectors")),
            "PageScraper", "SnapshotStore", "TrendPlotter", "TARGET_URL",
            "https://news.ycombinator.com/", 3600, "items found"),
    Profile("data_ml", ("csv", "dataset", "predict", "train", "classif", "regression",
                        "machine learning", "ml model", "forecast", "scikit", "sklearn"),
            "dataset", (("pandas", "loading and cleaning tabular data"),
                        ("scikit-learn", "training and scoring the model")),
            "ModelTrainer", "RunStore", "ScorePlotter", "DATASET_PATH", "data.csv", 86400,
            "score"),
    Profile("automation", ("file", "files", "folder", "directory", "backup", "rename",
                           "organize", "organise", "disk", "downloads"),
            "files", (),
            "FolderScanner", "ScanStore", "SizePlotter", "WATCH_DIR", ".", 600, "bytes"),
    Profile("web_api", ("api", "rest", "endpoint", "json", "webhook", "weather", "github",
                        "exchange rate", "price", "feed", "http"),
            "json", (("httpx", "async HTTP requests to the API"),),
            "ApiClient", "ReadingStore", "ReadingPlotter", "API_URL",
            "https://api.github.com/repos/python/cpython", 300, "value"),
)

#: Fallback when no keyword matches: an HTTP JSON poller.
DEFAULT_PROFILE = PROFILES[-1]

PLOT_WORDS = ("plot", "chart", "graph", "visualis", "visualiz", "dashboard")
CLASSIFY_WORDS = ("classif", "categor", "label", "spam", "fraud", "churn")
ETH_WORDS = ("ethereum", " eth ", "evm", "erc20", "erc-20", "web3")

AGENT_NOUNS = ("watcher", "monitor", "tracker", "scraper", "crawler", "bot", "analyzer",
               "analyser", "predictor", "classifier", "notifier", "collector", "fetcher",
               "downloader", "organizer", "organiser", "cleaner", "summarizer", "reporter",
               "checker", "alerter", "forecaster", "trainer", "poller", "logger", "dashboard",
               "service", "agent")

STOP_WORDS = frozenset("""a an the that which who and or of for to in on at by with from every
each all any some my our your their this these those is are be it its into as per then
them they logs log plots plot charts chart build create make write me please new hourly daily
minutes minute seconds second hours hour days day changes using via when telegram streamlit
fastapi aiogram web""".split())

VERBS = frozenset("""scrape crawl track train watch monitor fetch download collect analyze analyse
check get poll predict classify organize organise clean summarize summarise report alert
forecast backup notify read find list watches tracks monitors fetches scrapes collects
checks alerts sends dms plots serves exposes shows""".split())

SUFFIX = {"blockchain": "watcher", "scraping": "scraper", "data_ml": "trainer",
          "automation": "organizer", "web_api": "tracker"}

_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60,
          "mins": 60, "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600,
          "hour": 3600, "hours": 3600, "d": 86400, "day": 86400, "days": 86400}


def _words(command: str) -> list[str]:
    """Lower-case alphanumeric words of the command."""
    return re.findall(r"[a-z][a-z0-9]*", command.lower())


def choose_profile(command: str) -> Profile:
    """Pick the specialisation whose keywords best match the command."""
    text = f" {command.lower()} "
    best, best_hits = DEFAULT_PROFILE, 0
    for profile in PROFILES:
        hits = sum(1 for kw in profile.keywords if kw in text)
        if hits > best_hits:
            best, best_hits = profile, hits
    return best


def parse_interval(command: str, default: int) -> int:
    """Read ``every 60s`` / ``every 5 minutes`` / ``hourly`` from the command."""
    text = command.lower()
    match = re.search(r"every\s+(\d+)\s*([a-z]+)", text)
    if match and match.group(2) in _UNITS:
        return max(1, int(match.group(1)) * _UNITS[match.group(2)])
    match = re.search(r"every\s+(second|minute|hour|day)\b", text)
    if match:
        return _UNITS[match.group(1)]
    for word, seconds in (("hourly", 3600), ("daily", 86400), ("minutely", 60)):
        if word in text:
            return seconds
    return default


def _content(word: str) -> bool:
    """True for words that can name a sub-agent (not stop words, verbs or numbers)."""
    return word not in STOP_WORDS and word not in VERBS and not word[0].isdigit() \
        and word not in _UNITS


def make_slug(command: str, domain: str = "") -> str:
    """Derive a snake_case slug such as ``wallet_watcher`` from the command."""
    words = _words(command)
    for i, word in enumerate(words):
        if word in AGENT_NOUNS:
            qualifier = next((w for w in reversed(words[:i]) if _content(w)), "") \
                or next((w for w in words[i + 1:] if _content(w) and w not in AGENT_NOUNS), "")
            return f"{qualifier}_{word}" if qualifier else f"{word}_agent"
    content = [w for w in words if _content(w)][:2]
    suffix = SUFFIX.get(domain, "agent")
    return "_".join([*content, suffix])[:40].strip("_")


def camel(slug: str) -> str:
    """``wallet_watcher`` -> ``WalletWatcher``."""
    return "".join(part.capitalize() for part in slug.split("_") if part)


def plan_spec(command: str) -> dict[str, Any]:
    """Return a spec dictionary (matching SubAgentSpec) for ``command``."""
    profile = choose_profile(command)
    text = f" {command.lower()} "
    slug = make_slug(command, profile.domain)
    main_class = camel(slug)
    wants_plot = any(w in text for w in PLOT_WORDS)
    interval = parse_interval(command, profile.interval)

    client = profile.client
    rpc_flavour = ""
    if profile.fetch == "rpc":
        rpc_flavour = "ethereum" if any(w in text for w in ETH_WORDS) else "solana"
        client = f"{rpc_flavour.capitalize()}{client}"
    if main_class in {client, profile.store, profile.plotter, "Settings"}:
        main_class += "Agent"
    task = ""
    if profile.fetch == "dataset":
        task = "classification" if any(w in text for w in CLASSIFY_WORDS) else "regression"

    deps = [{"package": p, "purpose": why} for p, why in profile.dependencies]
    if wants_plot:
        if not any(d["package"] == "pandas" for d in deps):
            deps.append({"package": "pandas", "purpose": "shaping the history for plotting"})
        deps.append({"package": "matplotlib", "purpose": "rendering the PNG chart"})

    upper = slug.upper()
    env_vars = [
        {"name": profile.target_env, "purpose": f"what to watch (default: "
                                                f"{profile.default_target or 'none'})",
         "required": not profile.default_target},
        {"name": f"{upper}_INTERVAL", "purpose": "seconds between runs", "required": False},
        {"name": f"{upper}_DATA_DIR", "purpose": "where history and charts are saved",
         "required": False},
    ]
    extra_env = {
        "rpc": ("SOLANA_RPC_URL" if rpc_flavour == "solana" else "ETH_RPC_URL",
                "JSON-RPC endpoint (a free public node by default)"),
        "html": ("CSS_SELECTOR", "CSS selector of the items to collect (default: h2)"),
        "json": ("VALUE_FIELD", "dotted path of the number to track in the JSON reply"),
        "dataset": ("TARGET_COLUMN", "name of the column to predict (default: target)"),
        "files": ("FILE_PATTERN", "glob of files to include (default: *)"),
    }[profile.fetch]
    env_vars.append({"name": extra_env[0], "purpose": extra_env[1], "required": False})

    classes = [
        {"name": "Settings", "responsibility": "runtime settings from environment variables",
         "methods": ["from_env"]},
        {"name": client, "responsibility": f"fetches one {profile.value_label} reading",
         "methods": ["fetch", "aclose"]},
        {"name": profile.store, "responsibility": "appends readings to history.jsonl",
         "methods": ["append", "load", "last"]},
    ]
    if wants_plot:
        classes.append({"name": profile.plotter, "responsibility": "draws history to a PNG",
                        "methods": ["plot"]})
    classes.append({"name": main_class,
                    "responsibility": "orchestrates fetch, change detection, storage, chart",
                    "methods": ["run_once", "run_forever", "close"]})

    name = slug.replace("_", " ").title()
    summary = (f"{name} — an async {profile.domain.replace('_', '/')} sub-agent that "
               f"{command.strip().rstrip('.')}.")
    return {
        "name": name,
        "slug": slug,
        "summary": summary,
        "command": command.strip(),
        "domain": profile.domain,
        "dependencies": deps,
        "env_vars": env_vars,
        "classes": classes,
        "entrypoint": "main",
        "schedule_seconds": interval,
        "inputs": [profile.target_env],
        "outputs": [f"{slug}_data/history.jsonl"] + ([f"{slug}_data/chart.png"]
                                                      if wants_plot else []),
        "notes": [f"fetch={profile.fetch}", *( [f"rpc={rpc_flavour}"] if rpc_flavour else []),
                  *([f"task={task}"] if task else [])],
        "delivery": infer_delivery(command).model_dump(),
    }
