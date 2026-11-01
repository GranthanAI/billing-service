"""The one place aiokafka's producer is used."""

import asyncio
from collections.abc import Mapping
from typing import Any

from aiokafka import AIOKafkaProducer


def to_kafka_headers(headers: Mapping[str, str | bytes]) -> list[tuple[str, bytes]]:
    return [(k, v if isinstance(v, bytes) else v.encode()) for k, v in headers.items()]


class KafkaProducer:
    """Idempotent, acks=all; one per process, started and stopped by the process lifespan."""

    def __init__(self, bootstrap_servers: str, client_id: str = "billing-service") -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers,
            client_id=client_id,
            enable_idempotence=True,
            acks="all",
            linger_ms=5,
        )

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        await self._producer.stop()

    async def send(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, str | bytes] | None = None,
    ) -> asyncio.Future[Any]:
        """Queue a record; the returned future resolves once the broker acknowledged it."""
        future: asyncio.Future[Any] = await self._producer.send(
            topic, value=value, key=key, headers=to_kafka_headers(headers or {})
        )
        return future

    async def send_and_wait(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, str | bytes] | list[tuple[str, bytes]] | None = None,
    ) -> None:
        hdrs = headers if isinstance(headers, list) else to_kafka_headers(headers or {})
        await self._producer.send_and_wait(topic, value=value, key=key, headers=hdrs)

    async def flush(self) -> None:
        await self._producer.flush()
