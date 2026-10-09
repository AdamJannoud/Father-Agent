"""Offline planning rules used by the mock provider."""

from __future__ import annotations

import pytest

from father_agent.heuristics import choose_profile, make_slug, parse_interval, plan_spec


@pytest.mark.parametrize(("command", "domain", "slug"), [
    ("a Solana wallet watcher that logs balance changes every 60s and plots them",
     "blockchain", "wallet_watcher"),
    ("scrape hacker news headlines hourly", "scraping", "hacker_news_scraper"),
    ("train a classifier on a churn csv dataset", "data_ml", "churn_classifier"),
    ("organize my downloads folder by file type", "automation", "downloads_folder_organizer"),
    ("track the bitcoin price API every 5 minutes", "web_api", "bitcoin_price_tracker"),
])
def test_profile_and_slug(command: str, domain: str, slug: str) -> None:
    """Commands from each specialisation map to the right profile and slug."""
    profile = choose_profile(command)
    assert profile.domain == domain
    assert make_slug(command, profile.domain) == slug


@pytest.mark.parametrize(("command", "seconds"), [
    ("every 60s", 60), ("every 5 minutes", 300), ("every 2 h", 7200), ("hourly", 3600),
    ("every day", 86400), ("no schedule here", 42),
])
def test_parse_interval(command: str, seconds: int) -> None:
    """Intervals are read from the command, with a default."""
    assert parse_interval(command, 42) == seconds


def test_ethereum_and_classification_flavours() -> None:
    """Ethereum wallets use eth_getBalance; churn data is a classification task."""
    eth = plan_spec("an ethereum wallet monitor")
    assert any(c["name"] == "EthereumBalanceClient" for c in eth["classes"])
    ml = plan_spec("predict churn from a csv dataset")
    assert "task=classification" in ml["notes"]
