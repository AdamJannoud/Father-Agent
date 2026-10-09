"""RSS / Atom template for the ``rss`` capability pack."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import feedparser
import httpx

logger = logging.getLogger(__name__)


class FeedReader:
    """Downloads one feed with httpx and parses it with feedparser."""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        """Remember the feed URL."""
        self.url = url
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def entries(self) -> list[dict[str, Any]]:
        """Title, link and published date of every entry."""
        try:
            response = await self._client.get(self.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("feed download failed: %s", exc)
            return []
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        return [{"title": e.get("title", ""), "link": e.get("link", ""),
                 "published": e.get("published", "")} for e in parsed.entries]

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
