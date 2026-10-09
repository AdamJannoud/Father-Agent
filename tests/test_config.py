"""Config: free-only endpoints, placeholders, parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from father_agent.config import Config, check_free_endpoint, mask
from father_agent.errors import ConfigError


def test_defaults_are_keyless_and_free(config: Config) -> None:
    """With no keys, nothing remote is ready and endpoints are the free ones."""
    assert not config.remote_ready
    assert config.groq_base_url == "https://api.groq.com/openai/v1"
    assert config.hf_base_url == "https://router.huggingface.co/v1"


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://api.anthropic.com",
                                 "http://api.groq.com/openai/v1", "https://evil.example/v1"])
def test_paid_or_unknown_endpoints_are_refused(url: str) -> None:
    """Anything not on the free allow-list (or not https) is a ConfigError."""
    with pytest.raises(ConfigError):
        check_free_endpoint(url)


def test_env_override_to_paid_endpoint_fails_load(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """Pointing GROQ_BASE_URL at OpenAI is refused at load time."""
    monkeypatch.setenv("GROQ_BASE_URL", "https://api.openai.com/v1")
    with pytest.raises(ConfigError, match="only free"):
        Config.load(env_file=tmp_path / "none.env")


def test_local_llm_only_on_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A local llama.cpp server is accepted on localhost and refused elsewhere."""
    monkeypatch.setenv("FATHER_LOCAL_LLM_URL", "http://127.0.0.1:8080/v1")
    assert Config.load(env_file=tmp_path / "x").has_key("local")
    monkeypatch.setenv("FATHER_LOCAL_LLM_URL", "http://10.0.0.5:8080/v1")
    with pytest.raises(ConfigError):
        Config.load(env_file=tmp_path / "x")


def test_placeholders_count_as_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The .env.example placeholder values never count as a key."""
    monkeypatch.setenv("GROQ_API_KEY", "your-groq-key-here")
    monkeypatch.setenv("HF_TOKEN", "<hf token>")
    config = Config.load(env_file=tmp_path / "x")
    assert config.groq_api_key == "" and config.hf_token == ""


def test_env_file_is_read(tmp_path: Path) -> None:
    """Values in a .env file are loaded."""
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=gsk_testvalue_1234567890\nFATHER_MAX_RETRIES=5\n")
    config = Config.load(env_file=env)
    assert config.groq_api_key == "gsk_testvalue_1234567890"
    assert config.max_retries == 5
    assert config.env_file == env


def test_bad_numbers_and_providers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Garbage numeric settings and unknown provider names are clear errors."""
    monkeypatch.setenv("FATHER_MAX_RETRIES", "lots")
    with pytest.raises(ConfigError, match="FATHER_MAX_RETRIES"):
        Config.load(env_file=tmp_path / "x")
    monkeypatch.delenv("FATHER_MAX_RETRIES")
    monkeypatch.setenv("FATHER_PROVIDER_ORDER", "groq,openai")
    with pytest.raises(ConfigError, match="openai"):
        Config.load(env_file=tmp_path / "x")


def test_mask_never_reveals_more_than_four_chars() -> None:
    """mask() shows at most the last four characters."""
    assert mask("") == "missing"
    assert mask("gsk_abcdefghijklmnop") == "set (…mnop)"
    assert "abc" not in mask("short-abc")
