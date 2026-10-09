"""Retry and fall-through across free providers.

A 429 is retried on the same provider with backoff; when retries run out, or
the provider says the key or model is unusable, the chain moves to the next
provider instead of ending the run. Every call is bounded by an asyncio
timeout and a semaphore, so a hung free tier cannot freeze the factory.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..config import Config
from ..errors import AllProvidersFailedError, ConfigError, ProviderError
from .base import Completion, Message, Provider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

#: Called as ``on_event(message)`` so the CLI can show retries and switches.
EventHook = Callable[[str], None]


class ProviderChain:
    """An ordered list of providers tried one after another."""

    def __init__(self, providers: list[Provider], *, max_retries: int = 3,
                 timeout: float = 90.0, max_concurrency: int = 2, base_delay: float = 1.5,
                 max_delay: float = 20.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_event: EventHook | None = None) -> None:
        """Create the chain.

        Args:
            providers: Providers in priority order; must not be empty.
            max_retries: Attempts per provider for retryable failures.
            timeout: Hard ceiling in seconds for one provider call.
            max_concurrency: Concurrent in-flight calls across the chain.
            base_delay: First backoff delay in seconds (doubles each attempt).
            max_delay: Upper bound for any single backoff.
            sleep: Awaitable sleep, injectable so tests do not wait.
            on_event: Optional hook receiving human-readable retry/switch events.
        """
        if not providers:
            raise ConfigError("no providers configured")
        self.providers = providers
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self.on_event = on_event

    @property
    def primary(self) -> Provider:
        """The first provider, the one that will normally answer."""
        return self.providers[0]

    @property
    def is_mock_only(self) -> bool:
        """True when only the offline mock is available."""
        return all(p.is_mock for p in self.providers)

    def _emit(self, message: str) -> None:
        """Log a retry/switch event and forward it to the hook."""
        logger.warning(message)
        if self.on_event:
            try:
                self.on_event(message)
            except Exception:  # noqa: BLE001 - a broken hook must not break inference
                logger.exception("provider event hook failed")

    def _delay(self, attempt: int, error: ProviderError) -> float:
        """Backoff for ``attempt`` (1-based), honouring Retry-After when given."""
        if error.retry_after is not None:
            return min(error.retry_after, self.max_delay)
        return min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)

    async def complete(self, messages: list[Message], *, temperature: float = 0.2,
                       max_tokens: int = 4096) -> Completion:
        """Return the first successful completion from the chain.

        Raises:
            AllProvidersFailedError: When every provider has failed.
        """
        failures: list[str] = []
        for index, provider in enumerate(self.providers):
            for attempt in range(1, self.max_retries + 1):
                try:
                    async with self._semaphore:
                        result = await asyncio.wait_for(
                            provider.complete(messages, temperature=temperature,
                                              max_tokens=max_tokens),
                            timeout=self.timeout,
                        )
                    logger.info("%s → ok (attempt %d)", provider.label, attempt)
                    return result
                except TimeoutError:
                    error = ProviderError(f"{provider.label} exceeded {self.timeout:.0f}s",
                                          provider=provider.name, retryable=True)
                except ProviderError as exc:
                    error = exc
                except Exception as exc:  # noqa: BLE001 - surface anything as a provider failure
                    logger.exception("unexpected error from %s", provider.label)
                    error = ProviderError(f"{provider.label} crashed: {exc}",
                                          provider=provider.name)

                status = f"{error.status}" if error.status else "error"
                if error.retryable and attempt < self.max_retries:
                    delay = self._delay(attempt, error)
                    self._emit(f"{provider.label} → {status}, waiting {delay:.0f}s "
                               f"(attempt {attempt + 1} of {self.max_retries})")
                    await self._sleep(delay)
                    continue
                failures.append(str(error))
                if index + 1 < len(self.providers):
                    nxt = self.providers[index + 1]
                    self._emit(f"{provider.label} → {status}: {error}; switching to {nxt.label}")
                else:
                    self._emit(f"{provider.label} → {status}: {error}")
                break
        raise AllProvidersFailedError("every provider failed: " + " | ".join(failures))

    async def check_all(self) -> dict[str, str]:
        """Check every provider concurrently; returns ``label -> status``."""
        async def one(p: Provider) -> tuple[str, str]:
            try:
                return p.label, await asyncio.wait_for(p.check(), timeout=min(self.timeout, 20))
            except TimeoutError:
                return p.label, "timed out"
            except Exception as exc:  # noqa: BLE001 - doctor must report, not crash
                return p.label, f"error: {exc}"

        results = await asyncio.gather(*(one(p) for p in self.providers))
        return dict(results)

    async def aclose(self) -> None:
        """Close every provider's network resources."""
        await asyncio.gather(*(p.aclose() for p in self.providers), return_exceptions=True)


def build_provider(name: str, config: Config, **kwargs: object) -> Provider:
    """Instantiate one provider by name from ``config``.

    Raises:
        ConfigError: When the provider is unknown or lacks its key.
    """
    if name == "mock":
        return MockProvider()
    if name == "groq":
        if not config.groq_api_key:
            raise ConfigError("GROQ_API_KEY is not set (see .env.example)")
        return OpenAICompatProvider("groq", config.groq_base_url, config.groq_model,
                                    config.groq_api_key, timeout=config.request_timeout,
                                    **kwargs)  # type: ignore[arg-type]
    if name == "huggingface":
        if not config.hf_token:
            raise ConfigError("HF_TOKEN is not set (see .env.example)")
        return OpenAICompatProvider("huggingface", config.hf_base_url, config.hf_model,
                                    config.hf_token, timeout=config.request_timeout,
                                    **kwargs)  # type: ignore[arg-type]
    if name == "local":
        if not config.local_llm_url:
            raise ConfigError("FATHER_LOCAL_LLM_URL is not set (local models are off by default)")
        return OpenAICompatProvider("local", config.local_llm_url, config.local_llm_model,
                                    timeout=config.request_timeout,
                                    **kwargs)  # type: ignore[arg-type]
    raise ConfigError(f"unknown provider {name!r}")


def build_chain(config: Config, choice: str = "auto", *,
                on_event: EventHook | None = None, warn: bool = True) -> ProviderChain:
    """Build the provider chain for a run.

    ``auto`` uses every keyed provider in ``FATHER_PROVIDER_ORDER``. With no
    keys at all it falls back to the offline mock (with a warning); with keys
    it appends the mock only when ``FATHER_ALLOW_MOCK_FALLBACK`` is on.
    """
    if choice != "auto":
        providers = [build_provider(choice, config)]
    else:
        providers = [build_provider(n, config) for n in config.provider_order
                     if n != "mock" and config.has_key(n)]
        if not providers:
            (logger.warning if warn else logger.debug)("no GROQ_API_KEY or HF_TOKEN set: using the offline mock provider "
                           "(deterministic templates). Add a free key to .env for real models.")
            providers = [MockProvider()]
        elif config.allow_mock_fallback or "mock" in config.provider_order:
            providers.append(MockProvider())
    return ProviderChain(providers, max_retries=config.max_retries,
                         timeout=config.request_timeout,
                         max_concurrency=config.max_concurrency, on_event=on_event)
