"""Kafka consumer template for the ``kafka`` capability pack.

kafka-python is synchronous, so each poll runs in a worker thread and the
agent's event loop stays free. Nothing here connects until ``poll`` is called.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from kafka import KafkaConsumer

logger = logging.getLogger(__name__)


class TopicConsumer:
    """Reads JSON messages from one Kafka topic."""

    def __init__(self, topic: str, servers: str, group: str = "father-agent") -> None:
        """Remember the connection settings; the consumer is created lazily."""
        self.topic = topic
        self.servers = [s.strip() for s in servers.split(",") if s.strip()]
        self.group = group
        self._consumer: KafkaConsumer | None = None

    def _open(self) -> KafkaConsumer:
        """Create the consumer on first use."""
        if self._consumer is None:
            self._consumer = KafkaConsumer(
                self.topic, bootstrap_servers=self.servers, group_id=self.group,
                value_deserializer=lambda raw: json.loads(raw.decode("utf-8")))
        return self._consumer

    async def poll(self, timeout_ms: int = 1000) -> list[dict[str, Any]]:
        """Return the messages that arrived within ``timeout_ms``."""
        try:
            batches = await asyncio.to_thread(self._open().poll, timeout_ms=timeout_ms)
        except Exception as exc:  # noqa: BLE001 - a broker error must not kill the agent
            logger.warning("kafka poll failed: %s", exc)
            return []
        return [record.value for records in batches.values() for record in records]

    async def aclose(self) -> None:
        """Close the consumer if it was opened."""
        if self._consumer is not None:
            await asyncio.to_thread(self._consumer.close)
            self._consumer = None
