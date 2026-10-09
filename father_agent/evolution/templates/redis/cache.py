"""Redis template for the ``redis`` capability pack (redis-py's asyncio client)."""

from __future__ import annotations

import json
import logging
from typing import Any

from redis import asyncio as aioredis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisCache:
    """A small JSON cache on top of Redis."""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        """Create the client; no connection is made until first use."""
        self._redis = aioredis.from_url(url)

    async def get(self, key: str) -> Any:
        """The cached value, or None."""
        try:
            raw = await self._redis.get(key)
        except RedisError as exc:
            logger.warning("redis get failed: %s", exc)
            return None
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl: int = 3600) -> None:
        """Cache ``value`` for ``ttl`` seconds."""
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        except RedisError as exc:
            logger.warning("redis set failed: %s", exc)

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._redis.aclose()
