"""Provider layer: OpenAI-compatible HTTP, retries, fall-through, chain building.

All HTTP goes through httpx.MockTransport, so nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from father_agent.config import Config
from father_agent.errors import AllProvidersFailedError, ConfigError
from father_agent.providers import MockProvider, OpenAICompatProvider, ProviderChain, build_chain
from father_agent.providers.chain import build_provider

MESSAGES = [{"role": "user", "content": "hi"}]


async def _no_sleep(_: float) -> None:
    return None


def ok_body(text: str = "hello") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def provider(name: str, handler, model: str = "m") -> OpenAICompatProvider:
    return OpenAICompatProvider(name, "https://api.groq.com/openai/v1", model, "key-123",
                                transport=httpx.MockTransport(handler))


def test_success_sends_bearer_and_parses_reply() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body("pong"))

    result = asyncio.run(provider("groq", handler).complete(MESSAGES))
    assert result.text == "pong" and result.label == "groq/m"
    assert seen["auth"] == "Bearer key-123"
    assert seen["path"] == "/openai/v1/chat/completions"
    assert seen["body"]["messages"] == MESSAGES


def test_reasoning_block_is_stripped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body("<think>{not json}</think>\n{\"a\": 1}"))

    assert asyncio.run(provider("groq", handler).complete(MESSAGES)).text == '{"a": 1}'


def test_429_retries_then_switches_provider() -> None:
    calls = {"groq": 0, "huggingface": 0}
    events: list[str] = []

    def limited(request: httpx.Request) -> httpx.Response:
        calls["groq"] += 1
        return httpx.Response(429, headers={"retry-after": "3"},
                              json={"error": {"message": "rate limit"}})

    def healthy(request: httpx.Request) -> httpx.Response:
        calls["huggingface"] += 1
        return httpx.Response(200, json=ok_body("from hf"))

    chain = ProviderChain([provider("groq", limited), provider("huggingface", healthy)],
                          max_retries=3, sleep=_no_sleep, on_event=events.append)
    result = asyncio.run(chain.complete(MESSAGES))
    assert result.provider == "huggingface" and result.text == "from hf"
    assert calls == {"groq": 3, "huggingface": 1}
    assert "waiting 3s (attempt 2 of 3)" in events[0]
    assert "switching to huggingface/m" in events[-1]


def test_auth_error_switches_without_retrying() -> None:
    calls = {"n": 0}

    def rejected(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    def healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body("backup"))

    chain = ProviderChain([provider("groq", rejected), provider("huggingface", healthy)],
                          sleep=_no_sleep)
    assert asyncio.run(chain.complete(MESSAGES)).text == "backup"
    assert calls["n"] == 1


def test_hung_provider_times_out_and_falls_through() -> None:
    class Hung(MockProvider):
        name = "groq"

        async def complete(self, messages, **kwargs):
            await asyncio.sleep(10)

    async def fast(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body("fast"))

    chain = ProviderChain([Hung(), provider("huggingface", fast)], max_retries=1,
                          timeout=0.05, sleep=_no_sleep)
    assert asyncio.run(chain.complete(MESSAGES)).text == "fast"


def test_every_provider_failing_raises() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    chain = ProviderChain([provider("groq", broken), provider("huggingface", broken)],
                          max_retries=2, sleep=_no_sleep)
    with pytest.raises(AllProvidersFailedError, match="every provider failed"):
        asyncio.run(chain.complete(MESSAGES))


def test_malformed_payload_is_a_provider_error() -> None:
    def weird(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    chain = ProviderChain([provider("groq", weird)], max_retries=1, sleep=_no_sleep)
    with pytest.raises(AllProvidersFailedError, match="unexpected payload"):
        asyncio.run(chain.complete(MESSAGES))


def test_check_reports_key_and_model_status() -> None:
    def models(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer key-123":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    assert asyncio.run(provider("groq", models).check()) == "key ok"
    assert "not listed" in asyncio.run(provider("groq", models, model="gone").check())


def test_check_accepts_gemini_models_prefix() -> None:
    def models(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "models/gemini-3.8-flash"}]})

    gemini = provider("gemini", models, model="gemini-3.8-flash")
    assert asyncio.run(gemini.check()) == "key ok"
    assert "not listed" in asyncio.run(provider("gemini", models, model="gone").check())


def test_build_chain_without_keys_is_mock_only(config: Config) -> None:
    chain = build_chain(config)
    assert chain.is_mock_only and chain.primary.label == "mock/offline-templates"


def test_build_chain_with_keys_orders_free_providers(config: Config) -> None:
    keyed = Config(**{**config.__dict__, "groq_api_key": "gsk_x", "hf_token": "hf_y",
                      "allow_mock_fallback": True})
    chain = build_chain(keyed)
    assert [p.name for p in chain.providers] == ["groq", "huggingface", "mock"]
    asyncio.run(chain.aclose())


def test_build_chain_includes_gemini_when_keyed(config: Config) -> None:
    keyed = Config(**{**config.__dict__, "groq_api_key": "gsk_x", "gemini_api_key": "AIza_z"})
    chain = build_chain(keyed)
    assert [p.name for p in chain.providers] == ["groq", "gemini"]
    gemini = chain.providers[1]
    assert isinstance(gemini, OpenAICompatProvider)
    assert gemini.label == "gemini/gemini-3.8-flash"
    asyncio.run(chain.aclose())


def test_gemini_posts_to_openai_compat_route(config: Config) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=ok_body("from gemini"))

    keyed = Config(**{**config.__dict__, "gemini_api_key": "AIza_z"})
    gemini = build_provider("gemini", keyed, transport=httpx.MockTransport(handler))
    result = asyncio.run(gemini.complete(MESSAGES))
    assert result.text == "from gemini" and result.provider == "gemini"
    assert seen["url"] == ("https://generativelanguage.googleapis.com/v1beta/openai"
                           "/chat/completions")
    assert seen["auth"] == "Bearer AIza_z"


def test_forcing_an_unkeyed_provider_is_a_config_error(config: Config) -> None:
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        build_chain(config, "groq")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        build_chain(config, "gemini")
