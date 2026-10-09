"""One HTTP client for every OpenAI-compatible free endpoint.

Groq's free tier, Hugging Face's inference router and a local llama.cpp
server all speak the same ``/chat/completions`` protocol, so a single class
serves all three. This module talks plain HTTPS with httpx; no vendor SDK
is involved, and :func:`father_agent.config.check_free_endpoint` has already
refused any host that is not free.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..errors import ProviderError
from .base import Completion, Message, Provider

logger = logging.getLogger(__name__)

#: Status codes worth retrying on the same provider.
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}


def _retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header in seconds, if present and numeric."""
    raw = response.headers.get("retry-after", "")
    try:
        return max(0.0, float(raw)) if raw else None
    except ValueError:
        return None


def _error_detail(response: httpx.Response) -> str:
    """Best-effort extraction of an error message from a JSON error body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or err)[:200]
        return str(err)[:200]
    return str(body)[:200]


class OpenAICompatProvider(Provider):
    """Chat completions against an OpenAI-compatible base URL."""

    def __init__(self, name: str, base_url: str, model: str, api_key: str = "", *,
                 timeout: float = 90.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Create the provider.

        Args:
            name: Short provider name (``groq``, ``huggingface``, ``local``).
            base_url: Base URL ending in ``/v1`` (already checked as free).
            model: Model identifier for the backend.
            api_key: Bearer token; may be empty for a local server.
            timeout: HTTP timeout in seconds for one request.
            transport: Optional httpx transport, used by tests to stay offline.
        """
        self.name = name
        self.model = model
        self._base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json", "User-Agent": "father-agent/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers,
                                         timeout=timeout, transport=transport)

    async def complete(self, messages: list[Message], *, temperature: float = 0.2,
                       max_tokens: int = 4096) -> Completion:
        """POST ``/chat/completions`` and return the first choice's content."""
        payload: dict[str, Any] = {"model": self.model, "messages": messages,
                                   "temperature": temperature, "max_tokens": max_tokens}
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.label} timed out", provider=self.name,
                                retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.label} request failed: {exc}", provider=self.name,
                                retryable=True) from exc

        if response.status_code != 200:
            detail = _error_detail(response)
            raise ProviderError(
                f"{self.label} → HTTP {response.status_code}: {detail}",
                provider=self.name, status=response.status_code,
                retryable=response.status_code in RETRYABLE,
                retry_after=_retry_after(response),
            )
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.label} returned an unexpected payload",
                                provider=self.name, retryable=True) from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(f"{self.label} returned an empty reply", provider=self.name,
                                retryable=True)
        logger.debug("%s replied with %d chars", self.label, len(text))
        return Completion(text=text, provider=self.name, model=self.model)

    async def check(self) -> str:
        """GET ``/models`` to confirm the key works, without spending tokens."""
        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            logger.warning("%s check failed: %s", self.label, exc)
            return f"unreachable ({type(exc).__name__})"
        if response.status_code == 200:
            try:
                ids = {m.get("id") for m in response.json().get("data", [])}
            except (ValueError, AttributeError):
                return "reachable"
            if ids and self.model not in ids:
                return f"key ok, but model {self.model!r} is not listed"
            return "key ok"
        if response.status_code in (401, 403):
            return f"key rejected (HTTP {response.status_code})"
        return f"HTTP {response.status_code}: {_error_detail(response)}"

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        try:
            await self._client.aclose()
        except httpx.HTTPError as exc:
            logger.debug("error closing %s client: %s", self.label, exc)
